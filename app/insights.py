"""可视化用的数据（都按提问人权限过滤）：知识图谱、话题河流（经营驾驶舱在 cockpit.py，部门协作在 collab.py）。
只用到部门 / 话题 / 客户 / 产品 / 竞品 / 项目 / 区域这一层，不出现人名（用户要求）。数据来自 extract.py 的 fact 表。"""
import collections
import datetime as dt
import itertools
from zoneinfo import ZoneInfo

from . import access, db, rules, terms

TZ = ZoneInfo("Asia/Shanghai")
TYPES = ["客户", "平台", "产品", "竞品", "项目", "区域", "话题"]


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


