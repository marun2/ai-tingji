"""谁能看什么。

- 谁能打开这个应用：由企业中台控制（应用的可见范围，设为“董事局 + 指定同事”）
- 打开以后能看什么（本文件）：
    管理员 / 董事局（中台业务部门 board）→ 全部文件夹，包括待确认
    其他人（管理员在中台可见范围里指定的部门负责人）→ 只有「研发市场销售」里已确认、非敏感的会
- 管理员可以在「管理 → 人员」里给某个人单独改：全部可见 / 只看研发市场销售 / 不能看 / 管理员
- 第一个打开应用的人自动成为管理员（中台发布时可见范围默认“仅负责人”，第一个打开的就是负责人）
"""
import datetime as dt
import os
from urllib.parse import unquote

from starlette.exceptions import HTTPException
from starlette.requests import Request

from . import db, rules

ON_SERVER = bool(os.environ.get("ZT_APP"))  # 中台部署时会注入 ZT_APP
ROLE_LABEL = {"admin": "管理员", "all": "全部可见", "biz": "只看研发市场销售", "none": "不能看"}


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current(req: Request) -> dict:
    """当前用户 + 角色。没有中台身份头时（自己电脑上试用）当作本地管理员"""
    uid = req.headers.get("x-zt-user-id")
    if not uid and ON_SERVER:
        # 服务器上一定经过中台转发；没有身份（比如有人给应用开了公开访问）一律拒绝
        raise HTTPException(401, "请从企业中台打开")
    if not uid:
        # 本地试用：可以用 ?as=board / ?as=lead 模拟不同身份（只在没有中台身份头时生效）
        who = req.query_params.get("as") or req.cookies.get("tj_as") or "admin"
        fake = {"admin": ("local", "本地管理员", ""), "board": ("local-board", "本地董事局", "board"), "lead": ("local-lead", "本地负责人", "sales")}
        uid, name, dept = fake.get(who, fake["admin"])
    else:
        name = unquote(req.headers.get("x-zt-user-name", ""))
        dept = req.headers.get("x-zt-dept", "")
    with db.tx() as c:
        row = c.execute("SELECT * FROM member WHERE user_id = ?", (uid,)).fetchone()
        has_admin = c.execute("SELECT 1 FROM member WHERE role = 'admin' LIMIT 1").fetchone()
        if not row:
            role = None if has_admin else "admin"
            c.execute("INSERT INTO member (user_id, name, dept, role, last_seen) VALUES (?, ?, ?, ?, ?)", (uid, name, dept, role, now()))
            row = {"role": role}
        else:
            c.execute("UPDATE member SET name = ?, dept = ?, last_seen = ? WHERE user_id = ?", (name, dept, now(), uid))
    role = row.get("role") or ("all" if dept == "board" else "biz")
    return {"id": uid, "name": name, "dept": dept, "role": role, "role_label": ROLE_LABEL[role],
            "is_admin": role == "admin", "see_all": role in ("admin", "all")}


def require(u: dict) -> dict:
    if u["role"] == "none":
        raise HTTPException(403, "你没有查看听记的权限，请联系管理员")
    return u


def require_admin(u: dict) -> dict:
    if not u["is_admin"]:
        raise HTTPException(403, "只有管理员能做这个操作")
    return u


def folders_for(u: dict) -> list[str]:
    if u["see_all"]:
        return list(rules.FOLDERS)
    if u["role"] == "biz":
        return [rules.BIZ]
    return []


def meeting_filter(u: dict, alias: str = "m") -> tuple[str, list]:
    """拼到 WHERE 里的权限条件：返回 (SQL 片段, 参数)"""
    if u["see_all"]:
        return f"{alias}.status <> 'empty'", []
    if u["role"] == "biz":
        return (f"{alias}.status = 'ok' AND {alias}.sensitive = 0 AND EXISTS "
                f"(SELECT 1 FROM meeting_folder f WHERE f.meeting_id = {alias}.id AND f.folder = ?)"), [rules.BIZ]
    return "1 = 0", []


def can_view(u: dict, meeting_id: int) -> bool:
    cond, params = meeting_filter(u)
    return bool(db.q1(f"SELECT 1 FROM meeting m WHERE m.id = ? AND {cond}", (meeting_id, *params)))
