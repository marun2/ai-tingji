"""经营驾驶舱（2026-11-25 重做，设计说明见 docs/经营驾驶舱.md）

一句话定位：老板 / 负责人每周打开一次，3 分钟看懂“这段时间各部门会上说了什么、哪些事要我关注”，
每一条都能点开原会议、能一键追问。

结构是“卡片注册表”：每张卡片一个函数，拿到同一份上下文（权限范围、时间段、上一期对比段）返回自己的数据。
以后加卡片（比如接 KA 销售平台的销售达成、目标跟踪）只要写一个函数、在 CARDS 里登记，前端加一个渲染函数。

数据全部来自 extract.py 抽取的 fact 表，按看的人的权限过滤；面试和私人内容的会不算（insights._scope）。
"""
import collections
import datetime as dt
import re

from . import access, db, rules, terms
from .insights import _facts, _scope, _today

# ---------------------------------------------------------------- 时间段
PERIODS = [("week", "本周"), ("last_week", "上周"), ("14d", "近两周"), ("month", "本月"), ("30d", "近 30 天")]
DEFAULT_PERIOD = "14d"


def _period(key: str) -> dict:
    t = _today()
    mon = t - dt.timedelta(days=t.weekday())
    if key == "week":
        s, e = mon, t
        ps, pe = mon - dt.timedelta(days=7), mon - dt.timedelta(days=1)
    elif key == "last_week":
        s, e = mon - dt.timedelta(days=7), mon - dt.timedelta(days=1)
        ps, pe = s - dt.timedelta(days=7), s - dt.timedelta(days=1)
    elif key == "month":
        s, e = t.replace(day=1), t
        pe = s - dt.timedelta(days=1)
        ps = pe.replace(day=1)
    elif key == "30d":
        s, e = t - dt.timedelta(days=29), t
        ps, pe = s - dt.timedelta(days=30), s - dt.timedelta(days=1)
    else:
        key = "14d"
        s, e = t - dt.timedelta(days=13), t
        ps, pe = s - dt.timedelta(days=14), s - dt.timedelta(days=1)
    return {"key": key, "label": dict(PERIODS)[key], "start": s.isoformat(), "end": e.isoformat(),
            "prev_start": ps.isoformat(), "prev_end": pe.isoformat(), "days": (e - s).days + 1}


# ---------------------------------------------------------------- 文本相似（把同一件事的不同说法合在一起）
_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?（）()“”\"'《》【】\[\]—\-·…]+")


def _grams(s: str) -> set:
    s = _PUNCT.sub("", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _sim(a: set, b: set) -> float:
    return len(a & b) / (len(a | b) or 1)


def _cluster(rows: list[dict], th: float = 0.42) -> list[list[dict]]:
    groups: list[tuple[set, list]] = []
    for r in rows:
        g = _grams(r["text"])
        for gg, items in groups:
            if _sim(g, gg) >= th:
                items.append(r)
                gg |= g
                break
        else:
            groups.append((set(g), [r]))
    return [items for _g, items in groups]


# ---------------------------------------------------------------- 上下文
class Ctx:
    def __init__(self, u: dict, period: str, folder: str):
        self.u = u
        self.p = _period(period)
        self.allowed = access.folders_for(u)
        self.folder = folder if folder in self.allowed else ""
        self.w, self.a = self.scope(self.p["start"], self.p["end"])
        self.pw, self.pa = self.scope(self.p["prev_start"], self.p["prev_end"])
        self._folder_of = None

    def scope(self, start: str, end: str) -> tuple[str, list]:
        w, a = _scope(self.u, start=start, end=end)
        if self.folder:
            w += " AND EXISTS (SELECT 1 FROM meeting_folder mf WHERE mf.meeting_id = m.id AND mf.folder = ?)"
            a = [*a, self.folder]
        return w, a

    def facts(self, kind: str, w: str | None = None, a: list | None = None) -> list[dict]:
        w, a = (w, a) if w else (self.w, self.a)
        return db.q(f"SELECT f.text, f.label, f.value, f.unit, m.id AS meeting_id, m.title, m.start_time FROM fact f "
                    f"JOIN meeting m ON m.id = f.meeting_id WHERE {w} AND f.kind = ? ORDER BY m.start_time DESC", [*a, kind])

    def folder_of(self, mid: int) -> str:
        """一场会归哪个部门（看的人有权限的那个）"""
        if self._folder_of is None:
            self._folder_of = {}
            ws, as_ = self.scope(self.p["prev_start"], self.p["end"])
            for r in db.q(f"SELECT mf.meeting_id, mf.folder FROM meeting_folder mf JOIN meeting m ON m.id = mf.meeting_id WHERE {ws}", as_):
                if r["folder"] in self.allowed:
                    self._folder_of.setdefault(r["meeting_id"], r["folder"])
        return self._folder_of.get(mid, "")


def _src(r: dict) -> dict:
    return {"id": r["meeting_id"], "title": r["title"], "date": (r["start_time"] or "")[:10]}


# ---------------------------------------------------------------- 卡片
def card_attention(c: Ctx) -> dict:
    """需要关注：近期会上提到的风险和问题，同一件事合并；多场会、多个部门反复提、或上一期就有的排前面"""
    cur = c.facts("risk")
    old = c.facts("risk", *c.scope((dt.date.fromisoformat(c.p["start"]) - dt.timedelta(days=56)).isoformat(),
                                    (dt.date.fromisoformat(c.p["start"]) - dt.timedelta(days=1)).isoformat()))
    old_g = [(_grams(r["text"]), r) for r in old]
    items = []
    for g in _cluster(cur):
        mids = {r["meeting_id"] for r in g}
        folders = sorted({c.folder_of(m) for m in mids} - {""})
        gram = _grams(g[0]["text"])
        before = [r for gg, r in old_g if _sim(gram, gg) >= 0.42]
        last = max(r["start_time"] for r in g)
        score = len(mids) * 2 + len(folders) + (2 if before else 0) + (1 if last[:10] >= (_today() - dt.timedelta(days=3)).isoformat() else 0)
        items.append({"text": min((r["text"] for r in g), key=len) if len(g) > 1 else g[0]["text"],
                      "meetings": len(mids), "folders": folders,
                      "status": "持续" if before else "新出现",
                      "since": min(r["start_time"] for r in before)[:10] if before else min(r["start_time"] for r in g)[:10],
                      "last": last[:10], "sources": [_src(r) for r in sorted(g, key=lambda r: r["start_time"], reverse=True)[:5]],
                      "score": score})
    items.sort(key=lambda x: x["last"], reverse=True)
    items.sort(key=lambda x: -x["score"])
    return {"title": "需要关注", "desc": "会上提到的风险和问题；同一件事合并，多场会反复提、上一期就有的排前面",
            "total": len(items), "items": items[:8]}


def card_decisions(c: Ctx) -> dict:
    """定下来的事：按部门分组，同一件事只留一条"""
    by = collections.defaultdict(list)
    for g in _cluster(c.facts("decision"), 0.5):
        r = g[0]
        by[c.folder_of(r["meeting_id"]) or "其他"].append({"text": r["text"], **_src(r), "times": len({x["meeting_id"] for x in g})})
    groups = sorted(({"folder": f, "count": len(v), "items": v[:6]} for f, v in by.items()), key=lambda x: -x["count"])
    return {"title": "定下来的事", "desc": "会上形成的决议和明确安排，按部门分组", "total": sum(g["count"] for g in groups), "groups": groups}


def _norm_label(s: str) -> str:
    s = (s or "").strip()
    for k in ("销售额", "回款", "利润", "毛利", "产能", "铺市", "库存", "费用", "目标", "增长", "价格", "订单", "达成"):
        if k in s:
            return k if k not in ("铺市", "达成") else k + ("率" if "率" in s else "")
    return s[:8] or "其他"


def card_numbers(c: Ctx) -> dict:
    """会上提到的数字：按指标归类，每类看最近几条"""
    by = collections.defaultdict(list)
    seen = {}
    for r in c.facts("metric"):   # 同一个数字在几场会里重复说，只留最近一条，记次数
        k = (r["label"], r["value"], r["unit"] or "", _PUNCT.sub("", r["text"] or ""))
        if k in seen:
            seen[k]["times"] += 1
            continue
        seen[k] = {"value": r["value"], "unit": r["unit"] or "", "text": r["text"], "name": r["label"], "times": 1, **_src(r)}
        by[_norm_label(r["label"])].append(seen[k])
    groups = sorted(({"label": k, "count": sum(x["times"] for x in v), "items": v[:4]} for k, v in by.items()), key=lambda x: -x["count"])
    return {"title": "会上提到的数字", "desc": "来自会议发言，未经财务核实；按指标归类", "total": sum(g["count"] for g in groups), "groups": groups[:8]}


def card_momentum(c: Ctx) -> dict:
    """热度变化：这一期比之前提得明显多（升温）/ 以前常提、这一期几乎没提（降温）的客户、平台、产品、竞品、话题"""
    s, e, n = dt.date.fromisoformat(c.p["start"]), dt.date.fromisoformat(c.p["end"]), c.p["days"]
    base_start = s - dt.timedelta(days=n * 4)
    w, a = c.scope(base_start.isoformat(), c.p["end"])
    nodes, _f, times = _facts(w, a)
    recent, before = collections.Counter(), collections.Counter()
    rmids = collections.defaultdict(list)
    weeks = 8
    spark = collections.defaultdict(lambda: [0] * weeks)
    wk_end = e
    for mid, t in times.items():
        d = dt.date.fromisoformat(t[:10])
        for nd in nodes.get(mid, ()):
            (recent if d >= s else before)[nd] += 1
            if d >= s:
                rmids[nd].append((t, mid))
            i = weeks - 1 - (wk_end - d).days // 7
            if 0 <= i < weeks:
                spark[nd][i] += 1
    up, down = [], []
    for nd in set(recent) | set(before):
        r, b = recent[nd], before[nd] / 4
        item = {"type": nd[0], "name": nd[1], "recent": r, "before": round(b, 1), "spark": spark[nd]}
        if r >= 2 and r >= b * 1.8 + 1:
            up.append({**item, "lift": round((r + 1) / (b + 1), 1)})
        elif b >= 1.5 and r <= b * 0.34:
            down.append({**item, "lift": round((r + 1) / (b + 1), 2)})
    up.sort(key=lambda x: (-x["lift"], -x["recent"]))
    down.sort(key=lambda x: (x["lift"], -x["before"]))
    up, down = up[:8], down[:5]
    # 每行带上这一期提到它的会（点开就能看，不调大模型）
    want = {mid for x in up + down for _t, mid in sorted(rmids[(x["type"], x["name"])], reverse=True)[:6]}
    titles = {r["id"]: r for r in db.q(f"SELECT id, title, start_time FROM meeting WHERE id IN ({','.join('?' * len(want))})", list(want))} if want else {}
    for x in up + down:
        x["sources"] = [{"id": mid, "title": titles[mid]["title"], "date": (titles[mid]["start_time"] or "")[:10]}
                        for _t, mid in sorted(rmids[(x["type"], x["name"])], reverse=True)[:6] if mid in titles]
    return {"title": "热度变化", "desc": f"和之前 {n * 4} 天的平均水平比", "up": up, "down": down}


def card_pulse(c: Ctx) -> dict:
    """部门动态：每个部门开了多少会、比上一期多还是少、在聊什么、有几条风险和决议"""
    cnt = collections.Counter()
    prev = collections.Counter()
    for r in db.q(f"SELECT m.id FROM meeting m WHERE {c.w}", c.a):
        cnt[c.folder_of(r["id"])] += 1
    for r in db.q(f"SELECT m.id FROM meeting m WHERE {c.pw}", c.pa):
        prev[c.folder_of(r["id"])] += 1
    risks, decs = collections.Counter(), collections.Counter()
    for r in c.facts("risk"):
        risks[c.folder_of(r["meeting_id"])] += 1
    for r in c.facts("decision"):
        decs[c.folder_of(r["meeting_id"])] += 1
    nodes, _f, _t = _facts(c.w, c.a)
    top = collections.defaultdict(collections.Counter)
    for mid, ns in nodes.items():
        f = c.folder_of(mid)
        for nd in ns:
            top[f][nd] += 1
    items = []
    for f in (c.allowed if not c.folder else [c.folder]):
        if not cnt[f] and not prev[f]:
            continue
        items.append({"folder": f, "group": "子公司" if f in rules.SUBS else "上海食品", "meetings": cnt[f], "prev": prev[f],
                      "risks": risks[f], "decisions": decs[f], "top": [{"type": t, "name": nm} for (t, nm), _n in top[f].most_common(4)]})
    items.sort(key=lambda x: (x["group"] != "上海食品", -x["meetings"]))
    return {"title": "部门动态", "desc": "各部门这一期开了多少会、在聊什么", "items": items}


def card_rhythm(c: Ctx) -> dict:
    """会议节奏：近 12 周每周会议数（放在最下面，作背景参考）"""
    t = _today()
    wk0 = t - dt.timedelta(days=t.weekday())
    start = wk0 - dt.timedelta(weeks=11)
    w, a = c.scope(start.isoformat(), t.isoformat())
    trend = [0] * 12
    for r in db.q(f"SELECT m.start_time FROM meeting m WHERE {w}", a):
        i = (dt.date.fromisoformat(r["start_time"][:10]) - start).days // 7
        if 0 <= i < 12:
            trend[i] += 1
    return {"title": "会议节奏", "desc": "近 12 周每周会议数（深色是本周）", "trend": trend,
            "labels": [(start + dt.timedelta(weeks=i)).strftime("%m-%d") for i in range(12)]}


# 卡片注册表：顺序就是页面上的顺序。以后加卡片在这里登记（前端 COCKPIT_CARDS 里加同名渲染函数）
CARDS = [("attention", card_attention), ("momentum", card_momentum), ("decisions", card_decisions),
         ("numbers", card_numbers), ("pulse", card_pulse), ("rhythm", card_rhythm)]


def build(u: dict, period: str = DEFAULT_PERIOD, folder: str = "") -> dict:
    c = Ctx(u, period, folder)
    ids = [r["id"] for r in db.q(f"SELECT m.id FROM meeting m WHERE {c.w}", c.a)]
    prev_n = db.q1(f"SELECT COUNT(*) AS n FROM meeting m WHERE {c.pw}", c.pa)["n"]
    extracted = db.q1(f"SELECT COUNT(*) AS n FROM meeting m WHERE {c.w} AND m.extracted_at IS NOT NULL", c.a)["n"]
    w_all, a_all = _scope(u)
    latest = db.q1(f"SELECT MAX(m.start_time) AS t FROM meeting m WHERE {w_all}", a_all)["t"]
    imp = db.q1("SELECT created_at FROM import_batch WHERE status = 'done' ORDER BY id DESC LIMIT 1")
    imp_at = imp["created_at"] if imp else None
    days_since = (_today() - dt.date.fromisoformat(imp_at[:10])).days if imp_at else None
    cards = []
    for key, fn in CARDS:
        try:
            cards.append({"key": key, **fn(c)})
        except Exception as e:  # noqa: BLE001  一张卡片出错不影响其他卡片
            print(f"驾驶舱卡片 {key} 出错：", repr(e), flush=True)
            cards.append({"key": key, "title": key, "error": "这块暂时出错了"})
    folders_active = {c.folder_of(i) for i in ids} - {""}
    out = {
        "period": c.p, "periods": [{"key": k, "label": v} for k, v in PERIODS],
        "folders": c.allowed, "folder": c.folder,
        "summary": {"meetings": len(ids), "prev": prev_n, "depts": len(folders_active),
                    "attention": next((x.get("total", 0) for x in cards if x["key"] == "attention"), 0),
                    "decisions": next((x.get("total", 0) for x in cards if x["key"] == "decisions"), 0)},
        "coverage": {"meetings": len(ids), "extracted": extracted},
        "freshness": {"latest_meeting": (latest or "")[:10] or None, "last_import": (imp_at or "")[:10] or None,
                      "days_since_import": days_since, "stale": days_since is not None and days_since > 7},
        "cards": cards,
    }
    if u["see_all"]:
        out["pending"] = db.q1("SELECT COUNT(*) AS n FROM meeting WHERE status = 'pending'")["n"]
    return out


# ---------------------------------------------------------------- 使用情况（持续迭代用：看哪些卡片真有人点）
def track(card: str, action: str):
    card, action = (card or "")[:20], (action or "")[:20]
    if not card:
        return
    day = _today().isoformat()
    with db.tx() as cur:
        cur.execute("INSERT INTO ui_event (day, card, action, n) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT (day, card, action) DO UPDATE SET n = ui_event.n + 1" if db.IS_PG else
                    "INSERT INTO ui_event (day, card, action, n) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT (day, card, action) DO UPDATE SET n = n + 1", (day, card, action))


def track_stats(days: int = 30) -> list[dict]:
    since = (_today() - dt.timedelta(days=days)).isoformat()
    return db.q("SELECT card, action, SUM(n) AS n FROM ui_event WHERE day >= ? GROUP BY card, action ORDER BY SUM(n) DESC", (since,))
