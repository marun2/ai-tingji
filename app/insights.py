"""可视化用的数据（都按提问人权限过滤）：知识图谱、部门协作、话题河流、经营驾驶舱。
只用到部门 / 话题 / 客户 / 产品 / 竞品 / 项目 / 区域这一层，不出现人名（用户要求）。数据来自 extract.py 的 fact 表。"""
import collections
import datetime as dt
import itertools
from zoneinfo import ZoneInfo

from . import access, db, rules, terms

TZ = ZoneInfo("Asia/Shanghai")
TYPES = ["客户", "产品", "竞品", "项目", "区域", "话题"]


_NO_WORDS = ["面试", "候选人", "家庭教育", "育儿", "亲子", "家长会", "中考", "高考", "小升初", "升学", "保送", "私人"]
_NOT_INS = "NOT (COALESCE(m.mtype, '') = '面试' OR " + " OR ".join(f"m.title LIKE '%{x}%'" for x in _NO_WORDS) + ")"


def _today():
    return dt.datetime.now(TZ).date()


def _scope(u: dict, days: int | None = None, start: str = "", end: str = "") -> tuple[str, list]:
    access.require(u)
    cond, params = access.meeting_filter(u)
    # 面试和私人内容（子女教育讲座等）不进可视化，免得“招聘面试”“职业稳定性”这类词进图谱；
    # 人事的其他会照常算。知识库里这些会照样能看
    w, a = [cond, "m.start_time IS NOT NULL", _NOT_INS], list(params)
    if days:
        w.append("m.start_time >= ?"); a.append((_today() - dt.timedelta(days=days)).isoformat())
    if start:
        w.append("m.start_time >= ?"); a.append(start)
    if end:
        w.append("m.start_time <= ?"); a.append(end + " 23:59")
    return " AND ".join(w), a


def _facts(w: str, a: list) -> tuple[dict, dict, dict]:
    """返回 (会议 → 节点集合, 会议 → 文件夹, 会议 → 开始时间)"""
    nodes = collections.defaultdict(set)
    for r in db.q(f"SELECT f.meeting_id, f.kind, f.label, f.text FROM fact f JOIN meeting m ON m.id = f.meeting_id "
                  f"WHERE {w} AND f.kind IN ('entity', 'topic')", a):
        typ = terms.fix(r["label"] if r["kind"] == "entity" else "话题", r["text"])
        if typ:
            nodes[r["meeting_id"]].add((typ, r["text"]))
    folders = collections.defaultdict(list)
    for r in db.q(f"SELECT f.meeting_id, f.folder FROM meeting_folder f JOIN meeting m ON m.id = f.meeting_id WHERE {w}", a):
        folders[r["meeting_id"]].append(r["folder"])
    times = {r["id"]: r["start_time"] for r in db.q(f"SELECT m.id, m.start_time FROM meeting m WHERE {w}", a)}
    return nodes, folders, times


def graph(u: dict, days: int = 90, types: list[str] | None = None, limit: int = 90) -> dict:
    """知识图谱：部门 + 客户/产品/竞品/项目/区域/话题，同一场会里一起出现就连线"""
    types = [t for t in (types or TYPES) if t in TYPES]
    w, a = _scope(u, days)
    nodes, folders, times = _facts(w, a)
    cnt = collections.Counter()
    for mid, ns in nodes.items():
        for n in ns:
            if n[0] in types:
                cnt[n] += 1
    top = {n for n, _ in cnt.most_common(limit)}
    fcnt = collections.Counter(f for mid in times for f in folders.get(mid, []))
    allowed_folders = set(access.folders_for(u))
    edges = collections.Counter()
    for mid in times:
        ns = sorted(n for n in nodes.get(mid, ()) if n in top)
        for x, y in itertools.combinations(ns, 2):
            edges[(x, y)] += 1
        for f in folders.get(mid, []):
            if f in allowed_folders:
                for n in ns:
                    edges[(("部门", f), n)] += 1
    out_nodes = [{"id": f"部门:{f}", "type": "部门", "name": f, "count": c} for f, c in fcnt.items() if f in allowed_folders]
    out_nodes += [{"id": f"{t}:{n}", "type": t, "name": n, "count": cnt[(t, n)]} for (t, n) in top]
    ids = {n["id"] for n in out_nodes}
    out_edges = []
    for (x, y), c in edges.most_common(600):
        sx, sy = f"{x[0]}:{x[1]}", f"{y[0]}:{y[1]}"
        if c >= 2 and sx in ids and sy in ids:
            out_edges.append({"s": sx, "t": sy, "w": c})
    return {"nodes": out_nodes, "edges": out_edges, "meetings": len(times), "days": days}


def node_meetings(u: dict, node: str, days: int = 90) -> list[dict]:
    """点图谱里的一个点：列出相关会议"""
    typ, _, name = node.partition(":")
    w, a = _scope(u, days)
    if typ == "部门":
        sql = f"SELECT m.id, m.title, m.start_time FROM meeting m WHERE {w} AND EXISTS (SELECT 1 FROM meeting_folder f WHERE f.meeting_id = m.id AND f.folder = ?)"
        args = [*a, name]
    else:   # 类别可能被管理员改过，所以只按名称找
        sql = (f"SELECT m.id, m.title, m.start_time FROM meeting m WHERE {w} AND EXISTS "
               f"(SELECT 1 FROM fact f WHERE f.meeting_id = m.id AND f.kind IN ('entity', 'topic') AND f.text = ?)")
        args = [*a, name]
    return db.q(sql + " ORDER BY m.start_time DESC LIMIT 60", args)


def collab(u: dict, days: int = 90) -> dict:
    """部门协作：两个文件夹之间——同一场会同时归两边的次数，以及共同关心的话题/客户"""
    w, a = _scope(u, days)
    nodes, folders, times = _facts(w, a)
    allowed = set(access.folders_for(u))
    fcnt = collections.Counter()
    shared = collections.Counter()
    fnodes = collections.defaultdict(collections.Counter)
    for mid in times:
        fs = sorted(set(f for f in folders.get(mid, []) if f in allowed))
        for f in fs:
            fcnt[f] += 1
            for n in nodes.get(mid, ()):
                fnodes[f][n] += 1
        for x, y in itertools.combinations(fs, 2):
            shared[(x, y)] += 1
    links = []
    fl = list(fcnt)
    for x, y in itertools.combinations(sorted(fl), 2):
        common = set(fnodes[x]) & set(fnodes[y])
        topics = sorted(common, key=lambda n: -(fnodes[x][n] + fnodes[y][n]))[:6]
        score = shared[(x, y)] * 3 + len(common)
        if score:
            links.append({"s": x, "t": y, "meetings": shared[(x, y)], "common": len(common),
                          "top": [f"{n[1]}" for n in topics], "w": score})
    links.sort(key=lambda l: -l["w"])
    return {"folders": [{"name": f, "count": c, "group": "板块" if f == rules.BIZ else ("上海食品" if f in rules.FUNC else "子公司"),
                         "top": [n[1] for n, _ in fnodes[f].most_common(5)]} for f, c in fcnt.most_common()],
            "links": links[:60], "days": days}


def river(u: dict, weeks: int = 16, top: int = 8, kind: str = "话题") -> dict:
    """话题时间河流：最热的几个话题（或客户/产品）每周被提到的会议数"""
    today = _today()
    start = today - dt.timedelta(days=today.weekday() + 7 * (weeks - 1))
    w, a = _scope(u, start=start.isoformat())
    nodes, _folders, times = _facts(w, a)
    wk = lambda t: (dt.date.fromisoformat(t[:10]) - start).days // 7  # noqa: E731
    cnt = collections.Counter()
    series = collections.defaultdict(lambda: [0] * weeks)
    for mid, t in times.items():
        i = wk(t)
        if not 0 <= i < weeks:
            continue
        for n in nodes.get(mid, ()):
            if n[0] == kind:
                cnt[n[1]] += 1
                series[n[1]][i] += 1
    names = [n for n, _ in cnt.most_common(top)]
    labels = [(start + dt.timedelta(days=7 * i)).strftime("%m-%d") for i in range(weeks)]
    return {"weeks": labels, "series": [{"name": n, "values": series[n], "total": cnt[n]} for n in names], "kind": kind}


def cockpit(u: dict) -> dict:
    """经营驾驶舱：本周 vs 上周、12 周趋势、近期风险、升温最快的客户/话题"""
    today = _today()
    wk0 = today - dt.timedelta(days=today.weekday())
    w, a = _scope(u, start=(wk0 - dt.timedelta(weeks=11)).isoformat())
    rows = db.q(f"SELECT m.id, m.start_time FROM meeting m WHERE {w}", a)
    trend = [0] * 12
    for r in rows:
        i = (dt.date.fromisoformat(r["start_time"][:10]) - (wk0 - dt.timedelta(weeks=11))).days // 7
        if 0 <= i < 12:
            trend[i] += 1
    this_w, last_w = trend[-1], trend[-2]
    w14, a14 = _scope(u, days=14)
    risks = db.q(f"SELECT f.text, m.id AS meeting_id, m.title, m.start_time FROM fact f JOIN meeting m ON m.id = f.meeting_id "
                 f"WHERE {w14} AND f.kind = 'risk' ORDER BY m.start_time DESC LIMIT 12", a14)
    decisions = db.q(f"SELECT f.text, m.id AS meeting_id, m.title, m.start_time FROM fact f JOIN meeting m ON m.id = f.meeting_id "
                     f"WHERE {w14} AND f.kind = 'decision' ORDER BY m.start_time DESC LIMIT 12", a14)
    metrics = db.q(f"SELECT f.label, f.value, f.unit, f.text, m.id AS meeting_id, m.title, m.start_time FROM fact f JOIN meeting m ON m.id = f.meeting_id "
                   f"WHERE {w14} AND f.kind = 'metric' ORDER BY m.start_time DESC LIMIT 12", a14)
    # 升温：最近 2 周 vs 之前 6 周（按周平均）
    w8, a8 = _scope(u, days=56)
    nodes, folders, times = _facts(w8, a8)
    recent, before = collections.Counter(), collections.Counter()
    cut = (today - dt.timedelta(days=14)).isoformat()
    for mid, t in times.items():
        for n in nodes.get(mid, ()):
            (recent if t >= cut else before)[n] += 1
    rising = []
    for n, c in recent.items():
        if c >= 2:
            base = before[n] / 3  # 6 周 → 每 2 周平均
            rising.append({"type": n[0], "name": n[1], "recent": c, "before": round(base, 1), "lift": round(c / (base + 1), 2)})
    rising.sort(key=lambda x: -x["lift"])
    fw = collections.Counter(f for mid, t in times.items() if t >= wk0.isoformat() for f in folders.get(mid, []) if f in access.folders_for(u))
    last_import = db.q1("SELECT created_at FROM import_batch WHERE status = 'done' ORDER BY id DESC LIMIT 1")
    out = {"this_week": this_w, "last_week": last_w, "trend": trend,
           "trend_labels": [(wk0 - dt.timedelta(weeks=11 - i)).strftime("%m-%d") for i in range(12)],
           "folders_this_week": fw.most_common(), "risks": risks, "decisions": decisions, "metrics": metrics, "rising": rising[:10],
           "last_import": last_import["created_at"] if last_import else None, "latest_meeting": max(times.values()) if times else None}
    if u["see_all"]:
        out["pending"] = db.q1("SELECT COUNT(*) AS n FROM meeting WHERE status = 'pending'")["n"]
    return out
