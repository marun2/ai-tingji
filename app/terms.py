"""关键词修正：大模型抽取的客户/产品/竞品/项目/区域/话题，有时类别抽错（“每日博士”当成产品）、
或是太笼统没意义（“经销商”“全国”）。管理员在图谱里点一下就能改类别或不显示，规则存在表 term_rule，
只在显示时套用（图谱、驾驶舱、报告），不改原始抽取结果，随时能撤销。

另外两条自动规则：
- 和文件夹/主体同名的词（每日博士、大健康、均瑶云商……）本来就是部门节点，不再作为客户/产品出现
- 太笼统的词（DEFAULT_HIDE）默认不显示，管理员可撤销
"""
import threading

from . import access, db, rules

TYPES = ["客户", "产品", "竞品", "项目", "区域", "话题"]

# 2026-11-24 看真实图谱后定的初始规则（启动时一次性写入，之后以管理页为准）
DEFAULT_HIDE = ["经销商", "全国", "CVS", "客户", "渠道", "终端", "门店", "便利店", "商超", "KA", "线上", "线下", "电商", "公司", "总部"]

_cache = {"rules": None}
_lock = threading.Lock()


def _org_names() -> set[str]:
    names = set(rules.FOLDERS) | {n for _k, n in rules.ORGS} | {k for k, _n in rules.ORGS}
    return names | {"均瑶健康", "均瑶集团", "上海食品", "均瑶"}


def seed():
    if db.q1("SELECT 1 FROM setting WHERE k = 'mig_terms_v1'"):
        return
    with db.tx() as c:
        for n in DEFAULT_HIDE:
            if not c.execute("SELECT 1 FROM term_rule WHERE name = ?", (n,)).fetchone():
                c.execute("INSERT INTO term_rule (name, action, target, note, updated_at) VALUES (?, 'hide', '', '太笼统（初始规则）', ?)", (n, access.now()))
        c.execute("INSERT INTO setting (k, v) VALUES ('mig_terms_v1', '1')")
    reset()


def reset():
    with _lock:
        _cache["rules"] = None


def _rules() -> dict:
    with _lock:
        if _cache["rules"] is None:
            _cache["rules"] = {r["name"]: (r["action"], r["target"] or "") for r in db.q("SELECT name, action, target FROM term_rule")}
        return _cache["rules"]


def fix(typ: str, name: str) -> str | None:
    """(类别, 名称) → 修正后的类别；None 表示不显示"""
    if name in _org_names():
        return None
    r = _rules().get(name)
    if not r:
        return typ
    if r[0] == "hide":
        return None
    return r[1] if r[1] in TYPES else typ


def list_all() -> list[dict]:
    rows = db.q("SELECT name, action, target, note, updated_at FROM term_rule ORDER BY updated_at DESC, name")
    cnt = {}
    for r in db.q("SELECT text, kind, label, COUNT(DISTINCT meeting_id) AS n FROM fact WHERE kind IN ('entity', 'topic') GROUP BY text, kind, label"):
        cnt.setdefault(r["text"], []).append(("话题" if r["kind"] == "topic" else r["label"], r["n"]))
    for r in rows:
        r["original"] = "、".join(f"{t} {n} 场" for t, n in cnt.get(r["name"], [])) or "目前没有"
    return rows


def set_rule(name: str, action: str, target: str = "", note: str = ""):
    name = (name or "").strip()[:40]
    if not name:
        raise ValueError("缺少关键词")
    if action == "clear":
        with db.tx() as c:
            c.execute("DELETE FROM term_rule WHERE name = ?", (name,))
    elif action in ("hide", "type"):
        if action == "type" and target not in TYPES:
            raise ValueError("类别不对")
        with db.tx() as c:
            c.execute("DELETE FROM term_rule WHERE name = ?", (name,))
            c.execute("INSERT INTO term_rule (name, action, target, note, updated_at) VALUES (?, ?, ?, ?, ?)",
                      (name, action, target if action == "type" else "", note[:100], access.now()))
    else:
        raise ValueError("不支持的操作")
    reset()


def merge(name: str, into: str):
    """把一个词并到另一个词（加进同义词表，已抽取的名称马上统一）"""
    from . import synonyms
    name, into = (name or "").strip()[:40], (into or "").strip()[:40]
    if not name or not into or name == into:
        raise ValueError("要填一个不同的标准写法")
    with db.tx() as c:
        add = [name]
        old = c.execute("SELECT id, variants FROM synonym WHERE canonical = ?", (name,)).fetchone()
        if old:   # name 自己是某组的标准写法：那组的写法一起并过来
            add += synonyms._split(old["variants"])
            c.execute("DELETE FROM synonym WHERE id = ?", (old["id"],))
        row = c.execute("SELECT id, variants FROM synonym WHERE canonical = ?", (into,)).fetchone()
        if row:
            vs = synonyms._split(row["variants"])
            vs += [v for v in add if v not in vs and v != into]
            c.execute("UPDATE synonym SET variants = ? WHERE id = ?", ("，".join(vs), row["id"]))
        else:
            c.execute("INSERT INTO synonym (canonical, variants, note, enabled) VALUES (?, ?, '在图谱里合并', 1)", (into, "，".join(v for v in add if v != into)))
        c.execute("DELETE FROM term_rule WHERE name = ?", (name,))
    synonyms.reset()
    reset()
    synonyms.normalize_facts()
