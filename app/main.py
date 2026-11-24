"""AI听记 · 后端入口（Starlette）。

中台约定：
- 访问地址 https://apps.<公司域名>/ai-tingji/ ，中台完成飞书登录和可见范围校验后转发到本容器 8000 端口
- 当前用户在请求头 X-Zt-User-Id / X-Zt-User-Name（URL 编码）/ X-Zt-Dept（中台业务部门，董事局是 board）
- 必须提供 GET /healthz；前端请求接口一律用相对路径 api/xxx
"""
import asyncio
import json
import os
import pathlib
import re
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from urllib.parse import quote

from . import access, chunks, db, embed, exporter, ingest, llm, qa, rules

BASE = pathlib.Path(__file__).resolve().parent
UPLOADS = db.DATA_DIR / "uploads"
MAX_UPLOAD = 220 * 1024 * 1024   # 历史压缩包约 120MB
PAGE = 50


def ok(data, status=200):
    return JSONResponse(data, status_code=status)


async def body(req: Request) -> dict:
    try:
        return await req.json()
    except Exception:
        raise HTTPException(400, "请求格式不对")


def run(fn, *a):
    return asyncio.to_thread(fn, *a)


def audit(u: dict, action: str, detail: str = ""):
    with db.tx() as c:
        c.execute("INSERT INTO audit (user_name, action, detail, created_at) VALUES (?, ?, ?, ?)", (u["name"], action, detail[:500], access.now()))


def user(req: Request) -> dict:
    return access.current(req)


# ---------------------------------------------------------------- 基础
async def healthz(_):
    return ok({"ok": True})


async def me(req):
    u = await run(user, req)
    return ok({**u, "folders": access.folders_for(u), "llm": llm.configured() if u["is_admin"] else None})


# ---------------------------------------------------------------- 目录与会议
def _tree(u):
    access.require(u)
    cond, params = access.meeting_filter(u)
    rows = db.q(f"SELECT f.folder, substr(m.start_time, 1, 7) AS ym, COUNT(*) AS n FROM meeting m "
                f"JOIN meeting_folder f ON f.meeting_id = m.id WHERE {cond} GROUP BY f.folder, substr(m.start_time, 1, 7)", params)
    by = {}
    for r in rows:
        by.setdefault(r["folder"], {})[r["ym"] or "unknown"] = r["n"]
    allowed = access.folders_for(u)
    folders = []
    for f in allowed:
        months = sorted(by.get(f, {}).items(), key=lambda x: ("" if x[0] == "unknown" else x[0]), reverse=True)
        folders.append({"name": f, "group": "板块" if f == rules.BIZ else ("上海食品" if f in rules.FUNC else "子公司"),
                        "count": sum(n for _, n in months), "months": [{"ym": ym, "count": n} for ym, n in months]})
    out = {"folders": folders}
    if u["see_all"]:
        out["pending"] = db.q1("SELECT COUNT(*) AS n FROM meeting WHERE status = 'pending'")["n"]
        out["total"] = db.q1("SELECT COUNT(*) AS n FROM meeting WHERE status <> 'empty'")["n"]
    return out


async def tree(req):
    u = await run(user, req)
    return ok(await run(_tree, u))


def _attach_folders(items: list[dict]):
    if not items:
        return items
    ids = [m["id"] for m in items]
    rows = db.q(f"SELECT meeting_id, folder FROM meeting_folder WHERE meeting_id IN ({','.join('?' for _ in ids)})", ids)
    fm = {}
    for r in rows:
        fm.setdefault(r["meeting_id"], []).append(r["folder"])
    for m in items:
        m["folders"] = sorted(fm.get(m["id"], []), key=lambda f: rules.FOLDERS.index(f) if f in rules.FOLDERS else 99)
    return items


LIST_COLS = "m.id, m.title, m.creator, m.start_time, m.duration, m.entity, m.mtype, m.sensitive, m.status, m.keywords"


def _list(u, folder, month, status, page):
    access.require(u)
    cond, params = access.meeting_filter(u)
    where, args = [cond], list(params)
    if folder:
        if folder not in access.folders_for(u):
            raise HTTPException(403, "没有这个文件夹的权限")
        where.append("EXISTS (SELECT 1 FROM meeting_folder f2 WHERE f2.meeting_id = m.id AND f2.folder = ?)")
        args.append(folder)
    if month == "unknown":
        where.append("m.start_time IS NULL")
    elif month:
        where.append("substr(m.start_time, 1, 7) = ?")
        args.append(month)
    if status == "pending":
        if not u["see_all"]:
            raise HTTPException(403, "没有权限")
        where.append("m.status = 'pending'")
    w = " AND ".join(where)
    total = db.q1(f"SELECT COUNT(*) AS n FROM meeting m WHERE {w}", args)["n"]
    items = db.q(f"SELECT {LIST_COLS} FROM meeting m WHERE {w} ORDER BY COALESCE(m.start_time, '') DESC, m.id DESC LIMIT {PAGE} OFFSET ?",
                 (*args, max(0, page - 1) * PAGE))
    return {"total": total, "page": page, "page_size": PAGE, "items": _attach_folders(items)}


async def meetings(req):
    u = await run(user, req)
    p = req.query_params
    return ok(await run(_list, u, p.get("folder") or "", p.get("month") or "", p.get("status") or "", int(p.get("page") or 1)))


def _detail(u, mid):
    access.require(u)
    if not access.can_view(u, mid):
        raise HTTPException(404, "找不到这场会，或者你没有权限查看")
    m = db.q1("SELECT * FROM meeting WHERE id = ?", (mid,))
    _attach_folders([m])
    m["links"] = db.q("SELECT link_id, title FROM meeting_link WHERE meeting_id = ?", (mid,))
    if not u["see_all"]:
        for k in ("reason", "confidence", "classified_by", "org"):
            m.pop(k, None)
    return m


async def meeting_detail(req):
    u = await run(user, req)
    return ok(await run(_detail, u, int(req.path_params["id"])))


def _update(u, mid, data):
    access.require_admin(u)
    m = db.q1("SELECT id, title, status FROM meeting WHERE id = ?", (mid,))
    if not m:
        raise HTTPException(404, "找不到这场会")
    folders = [f for f in data.get("folders") or [] if f in rules.FOLDERS]
    sensitive = bool(data.get("sensitive"))
    if sensitive and rules.BIZ in folders:
        raise HTTPException(400, "敏感的会不能放进「研发市场销售」（部门负责人都能看到）")
    if not folders and m["status"] != "empty":
        raise HTTPException(400, "至少选一个文件夹")
    status = data.get("status") or ("ok" if m["status"] == "pending" else m["status"])
    if status not in ("ok", "pending", "empty"):
        raise HTTPException(400, "状态不对")
    with db.tx() as c:
        c.execute("UPDATE meeting SET sensitive = ?, status = ?, classified_by = 'manual', reason = ?, updated_at = ? WHERE id = ?",
                  (1 if sensitive else 0, status, f"{u['name']} 手动调整", access.now(), mid))
        c.execute("DELETE FROM meeting_folder WHERE meeting_id = ?", (mid,))
        c.executemany("INSERT INTO meeting_folder (meeting_id, folder) VALUES (?, ?)", [(mid, f) for f in folders])
    audit(u, "调整归类", f"{m['title']} → {'、'.join(folders)}{'（敏感）' if sensitive else ''} {status}")
    return _detail(u, mid)


async def meeting_update(req):
    u = await run(user, req)
    return ok(await run(_update, u, int(req.path_params["id"]), await body(req)))


def _confirm_all(u, ids):
    access.require_admin(u)
    ids = [int(i) for i in ids][:500]
    if not ids:
        return {"updated": 0}
    with db.tx() as c:
        n = c.execute(f"UPDATE meeting SET status = 'ok', classified_by = 'manual', updated_at = ? WHERE status = 'pending' AND id IN ({','.join('?' for _ in ids)})",
                      (access.now(), *ids)).rowcount
    audit(u, "批量确认", f"{n} 场")
    return {"updated": n}


async def confirm_batch(req):
    u = await run(user, req)
    return ok(await run(_confirm_all, u, (await body(req)).get("ids") or []))


# ---------------------------------------------------------------- 搜索（关键词；向量检索在第二步）
def _snippet(text: str, terms: list[str], width: int = 70) -> str:
    if not text:
        return ""
    pos = min((text.find(t) for t in terms if t in text), default=-1)
    if pos < 0:
        return ""
    s = max(0, pos - width)
    frag = re.sub(r"[*#>`|]+|\{origin:[^}]*\}", "", text[s:pos + width * 2]).replace("\n", " ")
    return ("…" if s else "") + re.sub(r"\s+", " ", frag) + "…"


def _search(u, qtext, folder):
    access.require(u)
    terms = [t for t in re.split(r"\s+", qtext.strip()) if t][:5]
    if not terms:
        return {"items": [], "total": 0}
    cond, params = access.meeting_filter(u)
    where, args = [cond], list(params)
    for t in terms:
        where.append("(m.title LIKE ? OR m.keywords LIKE ? OR m.summary LIKE ? OR m.transcript LIKE ? OR m.creator LIKE ?)")
        args += [f"%{t}%"] * 5
    if folder:
        if folder not in access.folders_for(u):
            raise HTTPException(403, "没有这个文件夹的权限")
        where.append("EXISTS (SELECT 1 FROM meeting_folder f2 WHERE f2.meeting_id = m.id AND f2.folder = ?)")
        args.append(folder)
    w = " AND ".join(where)
    rows = db.q(f"SELECT {LIST_COLS}, m.summary, m.transcript FROM meeting m WHERE {w} ORDER BY m.start_time DESC LIMIT 100", args)
    for r in rows:
        hit_title = all(t in r["title"] for t in terms)
        r["snippet"] = _snippet(r["summary"] or "", terms) or _snippet(r["transcript"] or "", terms)
        r["where"] = "标题" if hit_title else ("摘要" if _snippet(r["summary"] or "", terms) else "原文")
        r["score"] = (3 if hit_title else 0) + sum((r["summary"] or "").count(t) for t in terms) + 0.2 * sum((r["transcript"] or "").count(t) for t in terms)
        r.pop("summary")
        r.pop("transcript")
    rows.sort(key=lambda r: (-r["score"], r["start_time"]), reverse=False)
    return {"items": _attach_folders(rows[:60]), "total": len(rows), "terms": terms}


async def search(req):
    u = await run(user, req)
    return ok(await run(_search, u, req.query_params.get("q") or "", req.query_params.get("folder") or ""))


# ---------------------------------------------------------------- 导入（管理员）
async def import_upload(req):
    u = await run(user, req)
    access.require_admin(u)
    if ingest._job_lock.locked():
        raise HTTPException(409, "正在导入或分类，请等它完成")
    form = await req.form(max_part_size=MAX_UPLOAD)
    f = form.get("file")
    if not f or not getattr(f, "filename", ""):
        raise HTTPException(400, "请选择 Excel 文件")
    if not f.filename.lower().endswith((".xlsx", ".xlsm", ".zip", ".gz", ".json")):
        raise HTTPException(400, "只支持钉钉 AI听记导出的 .xlsx 表格、历史文件夹压缩包（.zip）或历史听记包（.json.gz）")
    with db.tx() as c:
        bid = db.insert(c, "import_batch", {"filename": f.filename, "uploaded_by": u["name"], "status": "running", "created_at": access.now()})
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-一-鿿]", "_", f.filename)[-80:]
    path = UPLOADS / f"{bid}-{safe}"
    import shutil
    await f.seek(0)
    with open(path, "wb") as out:          # 边读边写，大压缩包不占内存
        await run(shutil.copyfileobj, f.file, out, 4 << 20)
    if path.stat().st_size > MAX_UPLOAD:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "文件太大（超过 220MB）")
    audit(u, "导入", f.filename)
    ingest.start_import(bid, str(path))
    return ok({"id": bid})


def _batches(u):
    access.require_admin(u)
    rows = db.q("SELECT * FROM import_batch ORDER BY id DESC LIMIT 30")
    for r in rows:
        for k in ("stats", "progress"):
            r[k] = json.loads(r[k]) if r[k] else None
    return {"items": rows, "busy": ingest._job_lock.locked()}


async def imports(req):
    u = await run(user, req)
    return ok(await run(_batches, u))


def _batch_empty(u, bid):
    access.require_admin(u)
    return {"items": db.q("SELECT id, title, creator, start_time, duration, entity FROM meeting WHERE batch_id = ? AND status = 'empty' ORDER BY start_time DESC", (bid,))}


async def import_empty(req):
    u = await run(user, req)
    return ok(await run(_batch_empty, u, int(req.path_params["id"])))


# ---------------------------------------------------------------- 管理
async def settings_get(req):
    u = await run(user, req)
    access.require_admin(u)
    return ok({"llm": llm.public_settings(), "biz_subs": ingest.biz_subs(), "subs": rules.SUBS, "folders": rules.FOLDERS,
               "reclassify": ingest.reclassify_state()})


async def settings_put(req):
    u = await run(user, req)
    access.require_admin(u)
    data = await body(req)
    if "llm" in data:
        await run(llm.save_settings, data["llm"] or {})
        audit(u, "修改模型设置", f"{(data['llm'] or {}).get('llm_base_url', '')} {(data['llm'] or {}).get('llm_model', '')}")
    if "biz_subs" in data:
        subs = [s for s in data["biz_subs"] or [] if s in rules.SUBS]
        with db.tx() as c:
            v = json.dumps(subs, ensure_ascii=False)
            if db.IS_PG:
                c.execute("INSERT INTO setting (k, v) VALUES ('biz_subs', ?) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (v,))
            else:
                c.execute("INSERT OR REPLACE INTO setting (k, v) VALUES ('biz_subs', ?)", (v,))
        audit(u, "修改进研发市场销售的子公司", "、".join(subs))
    return await settings_get(req)


async def llm_test(req):
    u = await run(user, req)
    access.require_admin(u)
    try:
        return ok(await run(llm.test))
    except llm.LLMError as e:
        return ok({"ok": False, "error": str(e)})


async def reclassify(req):
    u = await run(user, req)
    access.require_admin(u)
    try:
        r = ingest.start_reclassify(u["name"])
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    audit(u, "重新分类", f"{r['total']} 场")
    return ok(r)


def _members(u):
    access.require_admin(u)
    rows = db.q("SELECT user_id, name, dept, role, last_seen FROM member ORDER BY last_seen DESC")
    for r in rows:
        eff = r["role"] or ("all" if r["dept"] == "board" else "biz")
        r["effective"] = eff
        r["effective_label"] = access.ROLE_LABEL[eff]
        r["auto"] = not r["role"]
    return {"items": rows, "roles": access.ROLE_LABEL}


async def members(req):
    u = await run(user, req)
    return ok(await run(_members, u))


def _member_set(u, uid, role):
    access.require_admin(u)
    if role not in ("", "admin", "all", "biz", "none"):
        raise HTTPException(400, "角色不对")
    if uid == u["id"] and role != "admin":
        raise HTTPException(400, "不能取消自己的管理员")
    with db.tx() as c:
        c.execute("UPDATE member SET role = ? WHERE user_id = ?", (role or None, uid))
    m = db.q1("SELECT name FROM member WHERE user_id = ?", (uid,))
    audit(u, "修改人员权限", f"{m['name'] if m else uid} → {access.ROLE_LABEL.get(role, '按部门自动')}")
    return _members(u)


async def member_set(req):
    u = await run(user, req)
    return ok(await run(_member_set, u, req.path_params["uid"], (await body(req)).get("role") or ""))


async def audit_log(req):
    u = await run(user, req)
    access.require_admin(u)
    return ok({"items": await run(db.q, "SELECT * FROM audit ORDER BY id DESC LIMIT 200")})


# ---------------------------------------------------------------- 提问
def _ask(u, data):
    n = db.q1("SELECT COUNT(*) AS n FROM ask_log WHERE user_id = ? AND created_at >= ?",
              (u["id"], (__import__("datetime").datetime.now() - __import__("datetime").timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")))["n"]
    if n >= 60:
        raise HTTPException(429, "提问太频繁了，一小时最多 60 个问题")
    try:
        return qa.ask(u, str(data.get("question") or ""), str(data.get("folder") or ""), data.get("history") or [])
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, llm.LLMError) as e:
        raise HTTPException(400, str(e))


async def ask_api(req):
    u = await run(user, req)
    return ok(await run(_ask, u, await body(req)))


async def ask_rate(req):
    u = await run(user, req)
    data = await body(req)
    try:
        await run(qa.rate, u, int(req.path_params["id"]), int(data.get("rating") or 0), str(data.get("comment") or ""))
    except PermissionError as e:
        raise HTTPException(404, str(e))
    return ok({"ok": True})


def _asks(u, only_bad):
    access.require_admin(u)
    w = "WHERE rating = -1" if only_bad else ""
    rows = db.q(f"SELECT id, user_name, scope, question, answer, rating, comment, seconds, created_at FROM ask_log {w} ORDER BY id DESC LIMIT 200")
    stat = db.q1("SELECT COUNT(*) AS n, SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS up, SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS down FROM ask_log")
    return {"items": rows, "stat": stat}


async def asks(req):
    u = await run(user, req)
    return ok(await run(_asks, u, req.query_params.get("bad") == "1"))


# ---------------------------------------------------------------- 导出 Word
async def export_api(req):
    u = await run(user, req)
    p = req.query_params
    ids = [int(x) for x in (p.get("ids") or "").split(",") if x.strip().isdigit()]
    try:
        name, data, n = await run(exporter.export, u, ids, p.get("folder") or "", p.get("month") or "")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(u, "导出 Word", f"{name}（{n} 场）")
    mt = "application/zip" if name.endswith(".zip") else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(data, media_type=mt, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------------------------------------------------------------- 向量模型状态
async def embed_state(req):
    u = await run(user, req)
    access.require_admin(u)
    return ok(embed.state())


async def embed_retry(req):
    u = await run(user, req)
    access.require_admin(u)
    embed.retry()
    embed.kick()
    return ok(embed.state())


# ---------------------------------------------------------------- 页面
async def index(_):
    html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    v = str(int(max(p.stat().st_mtime for p in (BASE / "static").glob("*.*"))))
    return Response(html.replace("__V__", v), media_type="text/html", headers={"Cache-Control": "no-cache"})


async def http_error(req, exc: HTTPException):
    if req.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return Response(str(exc.detail), status_code=exc.status_code, media_type="text/plain; charset=utf-8")


@asynccontextmanager
async def lifespan(_app):
    db.init()
    # 上次没跑完的导入（容器重启）标记为失败，避免一直显示“导入中”
    with db.tx() as c:
        c.execute("UPDATE import_batch SET status = 'failed', error = '服务重启，导入中断，请重新上传' WHERE status = 'running'")
    st = ingest.reclassify_state()
    if st.get("running"):
        ingest._set_state({**st, "running": False, "error": "服务重启，分类中断，请重新点“重新分类”"})
    try:
        n = chunks.backfill()
        if n:
            print(f"补切文本段：{n} 场会议", flush=True)
    except Exception as e:  # noqa: BLE001
        print("补切文本段失败：", e, flush=True)
    embed.start()
    yield


routes = [
    Route("/healthz", healthz),
    Route("/api/me", me),
    Route("/api/tree", tree),
    Route("/api/meetings", meetings),
    Route("/api/meetings/confirm", confirm_batch, methods=["POST"]),
    Route("/api/meetings/{id:int}", meeting_detail),
    Route("/api/meetings/{id:int}", meeting_update, methods=["PUT"]),
    Route("/api/search", search),
    Route("/api/imports", imports),
    Route("/api/imports", import_upload, methods=["POST"]),
    Route("/api/imports/{id:int}/empty", import_empty),
    Route("/api/admin/settings", settings_get),
    Route("/api/admin/settings", settings_put, methods=["PUT"]),
    Route("/api/admin/llm-test", llm_test, methods=["POST"]),
    Route("/api/admin/reclassify", reclassify, methods=["POST"]),
    Route("/api/admin/members", members),
    Route("/api/admin/members/{uid:str}", member_set, methods=["PUT"]),
    Route("/api/admin/audit", audit_log),
    Route("/api/ask", ask_api, methods=["POST"]),
    Route("/api/ask/{id:int}/rate", ask_rate, methods=["POST"]),
    Route("/api/admin/asks", asks),
    Route("/api/export", export_api),
    Route("/api/admin/embed", embed_state),
    Route("/api/admin/embed/retry", embed_retry, methods=["POST"]),
    Route("/", index),
    Mount("/static", StaticFiles(directory=BASE / "static"), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: http_error})
