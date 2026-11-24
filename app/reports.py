"""周报 / 月报。

- 两个版本：all = 全公司版（董事局、管理员看）；biz = 研发市场销售版（部门负责人看，只用研发市场销售里已确认、非敏感的会）
- 内容来自 extract.py 抽出来的决议 / 风险 / 数字 / 客户产品 / 话题，再由大模型写成报告；每条标出处（会议编号）
- 自动生成：每周一 8 点后生成上一周（周一到周日），每月 1 号 8 点后生成上个月；也可以在页面上手动生成
- 待办不追踪状态（用户决定），报告里不统计逾期
- 推送：暂不推送（时间以后再定）
"""
import collections
import datetime as dt
import json
import threading
import time
from zoneinfo import ZoneInfo

from . import access, db, llm, rules, terms

TZ = ZoneInfo("Asia/Shanghai")
_lock = threading.Lock()

SCOPES = {"all": "全公司版", "biz": "研发市场销售版"}

PROMPT = """你是均瑶健康的经营分析助手，根据一段时间内的会议记录摘要写{kind_name}。读者是公司老板和部门负责人，要简洁、有判断、能直接看重点。
用 Markdown，结构如下（没有内容的部分写“本期无”）：
## 一、本期概览
3～5 句话：开了多少会、主要在忙什么、最值得老板关注的 2～3 件事
## 二、经营数字
列出会上提到的关键数字（销售、回款、利润、产能、费用、目标等），写清是哪里/谁的；这些数字来自会议发言，未经财务核实
## 三、重要决议
按主题归并，每条一句话
## 四、风险与问题
按严重程度排序；连续出现的问题要点明
## 五、客户与竞品动态
## 六、下期关注
2～4 条建议
规则：
- 只依据给你的资料，不要编；每个要点后面用 [M编号] 标出处，如 [M12]，可以多个
- 不写人名
- {extra}"""


def _now():
    return dt.datetime.now(TZ)


def period(kind: str, ref: dt.date | None = None) -> tuple[str, str]:
    """上一个完整周期：周报 = 上周一到上周日；月报 = 上个月"""
    ref = ref or _now().date()
    if kind == "week":
        start = ref - dt.timedelta(days=ref.weekday() + 7)
        return start.isoformat(), (start + dt.timedelta(days=6)).isoformat()
    first = ref.replace(day=1)
    last_end = first - dt.timedelta(days=1)
    return last_end.replace(day=1).isoformat(), last_end.isoformat()


def _scope_cond(scope: str) -> tuple[str, list]:
    if scope == "biz":
        return ("m.status = 'ok' AND m.sensitive = 0 AND EXISTS (SELECT 1 FROM meeting_folder f WHERE f.meeting_id = m.id AND f.folder = ?)", [rules.BIZ])
    return "m.status <> 'empty'", []


def collect(scope: str, start: str, end: str) -> dict:
    cond, args = _scope_cond(scope)
    ms = db.q(f"SELECT m.id, m.title, m.start_time, m.entity, m.extracted_at FROM meeting m WHERE {cond} AND m.start_time >= ? AND m.start_time <= ? "
              f"ORDER BY m.start_time", (*args, start, end + " 23:59"))
    ids = [m["id"] for m in ms]
    facts = collections.defaultdict(list)
    folders = collections.Counter()
    if ids:
        ph = ",".join("?" for _ in ids)
        for f in db.q(f"SELECT meeting_id, kind, label, text, value, unit FROM fact WHERE meeting_id IN ({ph})", ids):
            facts[f["meeting_id"]].append(f)
        fm = collections.defaultdict(list)
        for r in db.q(f"SELECT meeting_id, folder FROM meeting_folder WHERE meeting_id IN ({ph})", ids):
            fm[r["meeting_id"]].append(r["folder"])
            folders[r["folder"]] += 1
        for m in ms:
            m["folders"] = fm.get(m["id"], [])
    topics = collections.Counter()
    ents = collections.defaultdict(collections.Counter)
    for fs in facts.values():   # 套用管理员的关键词修正（改类别 / 不显示）
        for f in fs:
            if f["kind"] in ("entity", "topic"):
                typ = terms.fix(f["label"] if f["kind"] == "entity" else "话题", f["text"])
                if typ == "话题":
                    topics[f["text"]] += 1
                elif typ:
                    ents[typ][f["text"]] += 1
    if scope == "biz":
        folders = collections.Counter({rules.BIZ: len(ms)})
    stats = {"meetings": len(ms), "extracted": sum(1 for m in ms if m["extracted_at"]), "folders": dict(folders.most_common()),
             "topics": topics.most_common(12), "entities": {k: v.most_common(8) for k, v in ents.items()},
             "by_day": dict(collections.Counter((m["start_time"] or "")[:10] for m in ms))}
    return {"meetings": ms, "facts": facts, "stats": stats}


def _material(data: dict) -> str:
    out = []
    for m in data["meetings"]:
        fs = data["facts"].get(m["id"], [])
        if not fs:
            continue
        lines = [f"[M{m['id']}] {(m['start_time'] or '')[:10]}《{m['title']}》（{'、'.join(m.get('folders') or []) or m['entity']}）"]
        for kind, name in (("decision", "决议"), ("risk", "问题"), ("metric", "数字")):
            items = [f for f in fs if f["kind"] == kind]
            if items:
                if kind == "metric":
                    lines.append(f"  {name}：" + "；".join(f"{f['label']} {f['value']}{f['unit']}（{f['text']}）" for f in items))
                else:
                    lines.append(f"  {name}：" + "；".join(f["text"] for f in items))
        ent = [f"{t}:{f['text']}" for f in fs if f["kind"] == "entity" and (t := terms.fix(f["label"], f["text"])) in ("客户", "平台", "竞品")]
        if ent:
            lines.append("  客户/竞品：" + "、".join(ent))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def generate(kind: str, scope: str, start: str, end: str, by: str) -> dict:
    if not llm.configured():
        raise RuntimeError("还没有配置大模型")
    data = collect(scope, start, end)
    st = data["stats"]
    kind_name = "周报" if kind == "week" else "月报"
    title = f"{'周报' if kind == 'week' else '月报'} {start} ~ {end}（{SCOPES[scope]}）"
    if not data["meetings"]:
        content = f"## 本期概览\n\n{start} 到 {end} 没有{'研发市场销售的' if scope == 'biz' else ''}会议记录（可能还没导入这段时间的听记）。"
    else:
        material = _material(data)
        if len(material) > 60000:  # 会太多时只保留信息最多的会
            ranked = sorted(data["meetings"], key=lambda m: -len(data["facts"].get(m["id"], [])))[:150]
            keep = {m["id"] for m in ranked}
            data = {**data, "meetings": [m for m in data["meetings"] if m["id"] in keep]}
            material = _material(data)
        extra = ("这是月报：在“本期概览”里加一句和上月相比的变化（如果资料能看出来），在“下期关注”里给出下个月重点。" if kind == "month" else "这是周报，写短一些。")
        head = (f"期间：{start} ~ {end}；会议 {st['meetings']} 场；各文件夹：" + "、".join(f"{k} {v}" for k, v in st["folders"].items())
                + f"；热门话题：" + "、".join(f"{t}({n})" for t, n in st["topics"][:8]))
        if st["extracted"] < st["meetings"]:
            head += f"\n注意：有 {st['meetings'] - st['extracted']} 场会还没抽取信息，下面资料不含它们。"
        content = llm.chat([{"role": "system", "content": PROMPT.replace("{kind_name}", kind_name).replace("{extra}", extra)},
                            {"role": "user", "content": f"{head}\n\n【会议资料】\n{material or '（这些会议还没有抽取出信息）'}"}],
                           max_tokens=3000, timeout=240, temperature=0.3, task="report")
    with db.tx() as c:
        c.execute("DELETE FROM report WHERE kind = ? AND scope = ? AND period_start = ?", (kind, scope, start))
        rid = db.insert(c, "report", {"kind": kind, "scope": scope, "period_start": start, "period_end": end, "title": title,
                                      "content": content, "stats": json.dumps(st, ensure_ascii=False), "created_by": by, "created_at": access.now()})
    return {"id": rid, "title": title}


# ---------------------------------------------------------------- 查看
def visible_scopes(u: dict) -> list[str]:
    if u["see_all"]:
        return ["all", "biz"]
    return ["biz"] if u["role"] == "biz" else []


def list_reports(u: dict) -> list[dict]:
    sc = visible_scopes(u)
    if not sc:
        return []
    return db.q(f"SELECT id, kind, scope, period_start, period_end, title, created_at, created_by FROM report WHERE scope IN ({','.join('?' for _ in sc)}) "
                f"ORDER BY period_start DESC, kind, scope LIMIT 200", sc)


def get_report(u: dict, rid: int) -> dict:
    r = db.q1("SELECT * FROM report WHERE id = ?", (rid,))
    if not r or r["scope"] not in visible_scopes(u):
        raise PermissionError("找不到这份报告")
    r["stats"] = json.loads(r["stats"] or "{}")
    # 出处 [M编号] → 会议标题（只给提问人能看的）
    import re
    ids = sorted({int(x) for x in re.findall(r"\[M(\d+)\]", r["content"] or "")})
    refs = {}
    if ids:
        cond, params = access.meeting_filter(u)
        for m in db.q(f"SELECT m.id, m.title, m.start_time FROM meeting m WHERE m.id IN ({','.join('?' for _ in ids)}) AND {cond}", (*ids, *params)):
            refs[m["id"]] = {"title": m["title"], "date": (m["start_time"] or "")[:10]}
    r["refs"] = refs
    return r


# ---------------------------------------------------------------- 自动生成
_job = {"running": False, "detail": ""}


def job_state() -> dict:
    return dict(_job)


def run_async(kind: str, start: str, end: str, by: str, scopes=("all", "biz")):
    if _lock.locked():
        raise RuntimeError("正在生成报告，请稍等")

    def go():
        with _lock:
            _job.update(running=True, detail=f"生成 {start} ~ {end}")
            try:
                for sc in scopes:
                    generate(kind, sc, start, end, by)
                _job.update(running=False, detail=f"已生成 {start} ~ {end}", error="")
            except Exception as e:  # noqa: BLE001
                _job.update(running=False, detail="", error=str(e)[:300])
    threading.Thread(target=go, daemon=True).start()


def _due() -> list[tuple[str, str, str]]:
    now = _now()
    if now.hour < 8:
        return []
    out = []
    for kind in ("week", "month"):
        s, e = period(kind)
        if s < "2026-08-14" and kind == "week":
            continue
        if not db.q1("SELECT 1 FROM report WHERE kind = ? AND period_start = ? AND scope = 'all'", (kind, s)):
            out.append((kind, s, e))
    return out


def scheduler():
    """后台每小时看一次：到点了、还没生成的就生成（只在配好大模型后）"""
    def loop():
        time.sleep(120)
        while True:
            try:
                if llm.configured() and not _lock.locked():
                    for kind, s, e in _due():
                        with _lock:
                            _job.update(running=True, detail=f"自动生成 {s} ~ {e}")
                            for sc in ("all", "biz"):
                                generate(kind, sc, s, e, "自动")
                            _job.update(running=False, detail=f"已自动生成 {s} ~ {e}", error="")
            except Exception as e:  # noqa: BLE001
                _job.update(running=False, error=f"自动生成失败：{str(e)[:200]}")
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True).start()
