"""同义词表：语音识别常把专有名词听错（“均瑶健康”→“君瑶健康”，“盒马”→“河马”），搜索和提问时把这些写法当成一回事。
管理员在「管理 → 同义词」里维护；第一次启动时放入下面的初始表（2026-11-24 从真实会议文字里统计出来的常见错写，括号里是出现次数）。
- 搜索 / 提问的关键词：任何一种写法都会扩展成整组一起找
- 抽取出的客户、产品等名称：统一成标准写法（图谱、报告里不会把“河马”和“盒马”当成两家）
"""
import re
import threading

from . import db

SEED = [
    ("均瑶健康", "君瑶健康,君耀健康,君耀,聚瑶健康,金瑶健康,俊瑶健康,菌瑶健康,均要健康,军药健康,军瑶健康,钧瑶健康,君药健康", "君瑶健康 438 次、君耀健康 355 次（用户确认是听错）"),
    ("均瑶集团", "君瑶集团,军药集团", "君瑶集团出现 136 次"),
    ("均瑶云商", "君瑶云商", ""),
    ("均瑶润盈", "君瑶润盈", ""),
    ("润盈", "润英,润营,润赢", ""),
    ("盒马", "河马,盒马鲜生,河马鲜生", "“河马”出现 725 次，基本都是盒马"),
    ("味动力", "胃动力,卫动力,微动力", "“胃动力”出现 650 次（用户确认是听错）"),
    ("每日博士", "美日博士,每日不是", "“美日博士”出现 166 次"),
    ("李子园", "李子源,李子元", ""),
    ("椰子水", "叶子水", ""),
    ("益生菌", "一生菌", ""),
    ("7-Eleven", "711,7-11,七十一,7-ELEVEn,柒拾壹", ""),
    ("衢州", "曲州,渠州", ""),
    ("锤石项目", "垂石项目,锤石", ""),
    ("美宜佳", "美宜家", ""),
    ("叮咚", "叮咚买菜", ""),
    ("小象", "小象超市", ""),
    ("朴朴", "朴朴超市", ""),
]
PENDING_NOTE: list[tuple[str, str, str]] = []

_cache = {"groups": None}
_lock = threading.Lock()


def seed():
    if db.q1("SELECT 1 FROM synonym LIMIT 1"):
        return
    with db.tx() as c:
        for canon, variants, note in SEED + PENDING_NOTE:
            c.execute("INSERT INTO synonym (canonical, variants, note, enabled) VALUES (?, ?, ?, 1)", (canon, variants, note))
    reset()


def migrate_v2():
    """用户 2026-11-24 确认：“君耀健康”是“均瑶健康”听错，“胃动力”是“味动力”听错。已上线的表按这个改一次，并把已抽取的名称统一"""
    if db.q1("SELECT 1 FROM setting WHERE k = 'mig_syn_v2'"):
        return
    with db.tx() as c:
        c.execute("DELETE FROM synonym WHERE canonical = '君耀健康'")
        row = c.execute("SELECT id, variants FROM synonym WHERE canonical = '均瑶健康'").fetchone()
        if row:
            vs = _split(row["variants"])
            for v in ("君耀健康", "君耀"):
                if v not in vs:
                    vs.append(v)
            c.execute("UPDATE synonym SET variants = ?, note = ? WHERE id = ?", ("，".join(vs), "君瑶健康 438 次、君耀健康 355 次（用户确认是听错）", row["id"]))
        c.execute("UPDATE synonym SET note = '“胃动力”出现 650 次（用户确认是听错）' WHERE canonical = '味动力'")
        c.execute("INSERT INTO setting (k, v) VALUES ('mig_syn_v2', '1')")
    reset()
    normalize_facts()


def migrate_v3():
    """2026-11-24 图谱里看到“微动力”单独成了一个产品，也是“味动力”听错，补进去"""
    if db.q1("SELECT 1 FROM setting WHERE k = 'mig_syn_v3'"):
        return
    with db.tx() as c:
        row = c.execute("SELECT id, variants FROM synonym WHERE canonical = '味动力'").fetchone()
        if row and "微动力" not in _split(row["variants"]):
            c.execute("UPDATE synonym SET variants = ? WHERE id = ?", ("，".join(_split(row["variants"]) + ["微动力"]), row["id"]))
        c.execute("INSERT INTO setting (k, v) VALUES ('mig_syn_v3', '1')")
    reset()
    normalize_facts()


def normalize_facts():
    """已抽取出来的客户/产品等名称，按当前同义词表统一成标准写法"""
    m = {}
    for g in groups():
        for v in g["variants"]:
            m[v] = g["canonical"]
    if not m:
        return 0
    n = 0
    with db.tx() as c:
        for v, canon in m.items():
            n += c.execute("UPDATE fact SET text = ? WHERE kind IN ('entity', 'topic') AND text = ?", (canon, v)).rowcount
    return n


def reset():
    with _lock:
        _cache["groups"] = None


def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，、;；\n]+", s or "") if x.strip()]


def groups() -> list[dict]:
    with _lock:
        if _cache["groups"] is None:
            rows = db.q("SELECT canonical, variants FROM synonym WHERE enabled = 1")
            _cache["groups"] = [{"canonical": r["canonical"].strip(), "variants": _split(r["variants"])} for r in rows if r["canonical"].strip()]
        return _cache["groups"]


def expand(term: str) -> list[str]:
    """一个搜索词 → 它所在组的全部写法（不在任何组就原样返回）"""
    t = term.strip()
    out = [t]
    for g in groups():
        forms = [g["canonical"], *g["variants"]]
        if any(t == f or (len(f) >= 2 and f in t) for f in forms):
            for f in forms:
                if f not in out:
                    out.append(t.replace(next(x for x in forms if x in t), f) if any(x in t for x in forms) else f)
    return list(dict.fromkeys(out))[:12]


def canonical(name: str) -> str:
    """抽取出的名称 → 标准写法"""
    n = (name or "").strip()
    for g in groups():
        if n == g["canonical"] or n in g["variants"]:
            return g["canonical"]
    return n


def list_all() -> list[dict]:
    return db.q("SELECT id, canonical, variants, note, enabled FROM synonym ORDER BY id")


def save_all(rows: list[dict]):  # noqa: C901
    with db.tx() as c:
        c.execute("DELETE FROM synonym")
        for r in rows[:500]:
            canon = str(r.get("canonical") or "").strip()[:40]
            if not canon:
                continue
            variants = "，".join(_split(str(r.get("variants") or "")))[:500]
            c.execute("INSERT INTO synonym (canonical, variants, note, enabled) VALUES (?, ?, ?, ?)",
                      (canon, variants, str(r.get("note") or "")[:200], 0 if r.get("enabled") is False else 1))
    reset()
    normalize_facts()
