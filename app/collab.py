"""部门协作（2026-11-25 重做，设计说明见 docs/部门协作.md）

要回答的问题：
1. 哪些事是跨部门的？——同一个客户 / 平台 / 产品 / 话题，好几个部门都在谈（跨部门议题）
2. 这些事有没有拉通？——有没有同时归到这几个部门的会（共同会议）；没有的标“各自在谈”，提示可能要对齐
3. 哪两个部门联系最紧、在一起谈什么？
4. 某个部门：和谁协作最多、它特有的关注点是什么

不调大模型，全部来自 fact 表和会议的文件夹归属；按看的人权限过滤；面试和私人内容的会不算（insights._scope）。
连线强度用“共同会议 × 3 + 共同关注（越少部门关心的事越加分）”，避免“春节备货”这种人人都谈的话题把所有部门连成一团。
"""
import collections
import itertools
import math

from . import access, db, rules
from .insights import _facts, _scope


def _group(f: str) -> str:
    return "板块" if f == rules.BIZ else ("子公司" if f in rules.SUBS else "上海食品")


def build(u: dict, days: int = 90) -> dict:
    w, a = _scope(u, days)
    nodes, folders, times = _facts(w, a)
    allowed = set(access.folders_for(u))
    titles = {r["id"]: r["title"] for r in db.q(f"SELECT m.id, m.title FROM meeting m WHERE {w}", a)}

    mf = {mid: sorted(set(f for f in folders.get(mid, []) if f in allowed)) for mid in times}
    fcnt = collections.Counter()
    joint_cnt = collections.Counter()
    item_dept = collections.defaultdict(collections.Counter)    # 关注点 → 部门 → 场次
    item_mids = collections.defaultdict(list)                   # 关注点 → [(时间, 会议)]
    pair_mids = collections.defaultdict(list)                   # (部门, 部门) → 共同会议
    for mid, fs in mf.items():
        for f in fs:
            fcnt[f] += 1
            if len(fs) > 1:
                joint_cnt[f] += 1
        for x, y in itertools.combinations(fs, 2):
            pair_mids[(x, y)].append(mid)
        for n in nodes.get(mid, ()):
            item_mids[n].append((times[mid], mid))
            for f in fs:
                item_dept[n][f] += 1

    depts = [f for f, _ in fcnt.most_common()]
    nd = max(1, len(depts))
    idf = {n: math.log((nd + 1) / (len(c) + 0.5)) for n, c in item_dept.items()}   # 越少部门谈的事权重越高
    src = lambda mid: {"id": mid, "title": titles.get(mid, ""), "date": (times.get(mid) or "")[:10]}  # noqa: E731

    # ---- 部门对
    pairs = []
    for x, y in itertools.combinations(sorted(depts), 2):
        shared = [n for n, c in item_dept.items() if c[x] and c[y]]
        weight = sum(min(item_dept[n][x], item_dept[n][y]) * max(idf[n], 0.05) for n in shared)
        joint = sorted(pair_mids.get((x, y), []), key=lambda m: times[m], reverse=True)
        score = len(joint) * 3 + weight
        if score < 1:
            continue
        top = sorted(shared, key=lambda n: -min(item_dept[n][x], item_dept[n][y]) * max(idf[n], 0.05))[:8]
        pairs.append({"a": x, "b": y, "joint": len(joint), "shared": len(shared), "score": round(score, 1),
                      "top": [{"type": n[0], "name": n[1], "a": item_dept[n][x], "b": item_dept[n][y]} for n in top],
                      "meetings": [src(m) for m in joint[:8]]})
    pairs.sort(key=lambda p: -p["score"])
    # 图上只画重要的线：每个部门最紧的 2 条 + 全局前 14 条，避免一团乱麻
    keep = set(range(min(14, len(pairs))))
    for f in depts:
        mine = [i for i, p in enumerate(pairs) if f in (p["a"], p["b"])][:2]
        keep.update(mine)
    for i, p in enumerate(pairs):
        p["on_graph"] = i in keep

    # ---- 跨部门议题：至少 2 个部门、每个部门至少 2 场会都在谈的事
    issues = []
    for n, c in item_dept.items():
        ds = [f for f, k in c.items() if k >= 2]
        if len(ds) < 2:
            continue
        ms = item_mids[n]
        dset = set(ds)
        joint = [mid for _t, mid in ms if len(dset & set(mf[mid])) >= 2]
        by_dept = {f: [src(mid) for _t, mid in sorted((x for x in ms if f in mf[x[1]]), reverse=True)[:4]] for f in ds}
        issues.append({"type": n[0], "name": n[1], "depts": sorted(({"name": f, "n": c[f]} for f in ds), key=lambda d: -d["n"]),
                       "total": len(ms), "joint": len(set(joint)), "last": max(t for t, _ in ms)[:10],
                       "status": "有共同会议" if joint else "各自在谈", "by_dept": by_dept,
                       "score": len(ds) * 2 + math.log1p(len(ms)) + (1.5 if not joint else 0)})
    issues.sort(key=lambda x: -x["score"])
    issues = issues[:40]

    # ---- 部门
    total_items = collections.Counter({n: sum(c.values()) for n, c in item_dept.items()})
    all_m = max(1, sum(fcnt.values()))
    dept_out = []
    for f in depts:
        mine = collections.Counter({n: c[f] for n, c in item_dept.items() if c[f]})
        # 特有关注：在这个部门出现的比例明显高于全公司平均
        special = sorted((n for n, k in mine.items() if k >= 2 and k / max(1, total_items[n]) >= 0.7),
                         key=lambda n: -mine[n])[:6]
        partners = sorted((p for p in pairs if f in (p["a"], p["b"])), key=lambda p: -p["score"])[:5]
        dept_out.append({"name": f, "group": _group(f), "count": fcnt[f], "joint": joint_cnt[f],
                         "joint_rate": round(joint_cnt[f] / fcnt[f], 2) if fcnt[f] else 0,
                         "top": [{"type": n[0], "name": n[1], "n": k} for n, k in mine.most_common(6)],
                         "special": [{"type": n[0], "name": n[1], "n": mine[n]} for n in special],
                         "partners": [{"name": p["b"] if p["a"] == f else p["a"], "joint": p["joint"], "shared": p["shared"],
                                       "top": [t["name"] for t in p["top"][:3]]} for p in partners]})
    gaps = sum(1 for x in issues if x["status"] == "各自在谈")
    joint_total = sum(1 for fs in mf.values() if len(fs) > 1)
    return {"days": days, "depts": dept_out, "pairs": pairs[:60], "issues": issues,
            "summary": {"depts": len(depts), "meetings": len(mf), "joint": joint_total,
                        "joint_rate": round(joint_total / len(mf), 3) if mf else 0,
                        "issues": len(issues), "gaps": gaps,
                        "top_pair": {"a": pairs[0]["a"], "b": pairs[0]["b"]} if pairs else None}}
