"""平台内提问：理解问题 → 按权限找相关内容（关键词 + 本地向量）→ 大模型只根据找到的内容回答，并标出处。

权限：先用 access.meeting_filter 圈出提问人能看的会议，检索只在这些会议里做，大模型看不到别的内容。
"""
import datetime as dt
import json
import re
import time
from zoneinfo import ZoneInfo

from . import access, db, embed, llm, rules

TZ = ZoneInfo("Asia/Shanghai")
MAX_CHUNKS = 14
MAX_CHARS = 14000
PER_MEETING = 3

UNDERSTAND = """你在帮均瑶健康的员工查公司会议记录（钉钉 AI听记）。把用户的问题整理成检索条件，只输出 JSON：
{"mode": "detail 或 list", "keywords": ["3～8 个最能命中相关会议的词：人名、客户、产品、项目、区域、专有名词，以及问题核心词；不要“会议”“情况”这类泛词"],
 "date_from": "YYYY-MM-DD 或空", "date_to": "YYYY-MM-DD 或空", "rewrite": "把指代补全后的完整问题"}
- mode=list：用户要的是“有哪些会/开了什么会/会议清单”这类列举；其他都是 detail
- 时间：今天是 {today}（{weekday}）。“本周”指本周一到今天，“上周”指上周一到上周日，“最近”默认最近 30 天；没提时间就留空
- 如果有对话历史，结合历史理解“它”“这个”等指代"""

ANSWER = """你是均瑶健康的会议知识助手。只能根据下面【会议资料】回答，资料里没有的不要编，也不要用常识补数字。
- 用中文，先给结论，再分点说明；涉及数字、日期、人名、决定的，必须照资料原样
- 每个要点后面用 [编号] 标出处，编号就是资料的编号，可以多个如 [2][5]
- 资料不够回答时，直接说“现有会议记录里没有找到……”，并说明找到了哪些相近的内容
- 不同会议说法不一致时，分别列出并标出处
- 资料里标“会议材料”的是会前准备的汇报材料（PPT/PDF），标“摘要”“原文”的是会上发言；两者数字不一致时都列出，并说明哪个是材料、哪个是发言
- 不要输出资料编号以外的链接"""


def _today():
    d = dt.datetime.now(TZ).date()
    return d, "一二三四五六日"[d.weekday()]


def understand(question: str, history: list[dict]) -> dict:
    today, wd = _today()
    hist = "\n".join(f"用户：{h.get('q', '')[:200]}\n助手：{h.get('a', '')[:300]}" for h in history[-3:])
    msg = (f"对话历史：\n{hist}\n\n" if hist else "") + f"问题：{question}"
    try:
        d = llm.parse_json(llm.chat([{"role": "system", "content": UNDERSTAND.replace("{today}", today.isoformat()).replace("{weekday}", "星期" + wd)},
                                     {"role": "user", "content": msg}], max_tokens=800, timeout=60, task="ask"))
    except Exception:  # noqa: BLE001 —— 理解失败就按原话检索
        d = {}
    kws = [str(k).strip() for k in d.get("keywords") or [] if str(k).strip()][:8]
    if not kws:
        kws = [w for w in re.split(r"[\s，。？?！!、,.]+", question) if len(w) >= 2][:6]
    def date(v):
        return v if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else ""
    return {"mode": "list" if d.get("mode") == "list" else "detail", "keywords": kws, "date_from": date(d.get("date_from")),
            "date_to": date(d.get("date_to")), "rewrite": str(d.get("rewrite") or question)[:300]}


def _scope_sql(u: dict, folder: str, q: dict) -> tuple[str, list]:
    cond, params = access.meeting_filter(u)
    where, args = [cond], list(params)
    if folder:
        if folder not in access.folders_for(u):
            raise PermissionError("没有这个文件夹的权限")
        where.append("EXISTS (SELECT 1 FROM meeting_folder f2 WHERE f2.meeting_id = m.id AND f2.folder = ?)")
        args.append(folder)
    if q.get("date_from"):
        where.append("m.start_time >= ?"); args.append(q["date_from"])
    if q.get("date_to"):
        where.append("m.start_time <= ?"); args.append(q["date_to"] + " 23:59")
    return " AND ".join(where), args


def _keyword_hits(w: str, args: list, kws: list[str]) -> dict[int, float]:
    if not kws:
        return {}
    from . import synonyms
    groups = [synonyms.expand(k) for k in kws]          # 每个词连同它的同义写法（语音识别错写）一起找
    forms = [f for g in groups for f in g][:40]
    like = " OR ".join("c.text LIKE ?" for _ in forms)
    rows = db.q(f"SELECT c.id, c.kind, c.text FROM chunk c JOIN meeting m ON m.id = c.meeting_id WHERE {w} AND ({like}) LIMIT 1500",
                (*args, *[f"%{k}%" for k in forms]))
    sc = {}
    for r in rows:
        s = 0.0
        for g in groups:
            n = sum(r["text"].count(f) for f in g)
            if n:
                s += 1 + min(n, 5) * 0.3        # 命中的词越多越靠前；同一个词多次出现加分有上限
        if r["kind"] == "summary":
            s *= 1.3
        sc[r["id"]] = s
    return sc


def _material_hits(mids: list[int], kws: list[str]) -> dict[int, float]:
    """会议材料（PDF）里的关键词命中；材料的文本段 meeting_id 是负的材料编号"""
    if not kws or not mids:
        return {}
    from . import synonyms
    groups = [synonyms.expand(k) for k in kws]
    forms = [f for g in groups for f in g][:40]
    like = " OR ".join("c.text LIKE ?" for _ in forms)
    rows = db.q(f"SELECT c.id, c.text FROM chunk c WHERE c.meeting_id IN ({','.join('?' for _ in mids)}) AND ({like}) LIMIT 800",
                (*[-m for m in mids], *[f"%{k}%" for k in forms]))
    sc = {}
    for r in rows:
        s = sum(1 + min(sum(r["text"].count(f) for f in g), 5) * 0.3 for g in groups if any(f in r["text"] for f in g))
        sc[r["id"]] = s * 1.1     # 材料是会前准备的正式内容，略微靠前
    return sc


def retrieve(u: dict, question: str, folder: str, q: dict) -> list[dict]:
    from . import materials
    w, args = _scope_sql(u, folder, q)
    kw = _keyword_hits(w, args, q["keywords"])
    mat_ids = materials.visible_ids(u, folder, q.get("date_from") or "", q.get("date_to") or "")
    kw.update(_material_hits(mat_ids, q["keywords"]))
    vec = []
    if embed.ready():
        allowed = {r["id"] for r in db.q(f"SELECT m.id FROM meeting m WHERE {w}", args)} | {-m for m in mat_ids}
        if allowed:
            try:
                qv = embed.embed([q["rewrite"] or question], query=True)[0]
                vec = embed.search(qv, allowed, top=60)
            except Exception as e:  # noqa: BLE001
                print("向量检索失败：", e, flush=True)
    # 两路结果按名次融合（RRF）
    fused: dict[int, float] = {}
    for rank, (cid, _s) in enumerate(sorted(kw.items(), key=lambda x: -x[1])[:60]):
        fused[cid] = fused.get(cid, 0) + 1 / (60 + rank)
    for rank, (cid, _s) in enumerate(vec):
        fused[cid] = fused.get(cid, 0) + 1 / (60 + rank)
    if not fused:
        return []
    ids = sorted(fused, key=lambda c: -fused[c])[:60]
    rows = {r["id"]: r for r in db.q(
        f"SELECT c.id, c.meeting_id, c.kind, c.text, m.title, m.start_time FROM chunk c JOIN meeting m ON m.id = c.meeting_id "
        f"WHERE c.id IN ({','.join('?' for _ in ids)})", ids)}
    for r in db.q(f"SELECT c.id, c.meeting_id, c.kind, c.seq, c.text, mt.filename AS title, mt.doc_date AS start_time FROM chunk c "
                  f"JOIN material mt ON mt.id = -c.meeting_id WHERE c.meeting_id < 0 AND c.id IN ({','.join('?' for _ in ids)})", ids):
        if -r["meeting_id"] in mat_ids:
            rows[r["id"]] = r
    out, per, chars = [], {}, 0
    for cid in ids:
        r = rows.get(cid)
        if not r or per.get(r["meeting_id"], 0) >= PER_MEETING:
            continue
        if chars + len(r["text"]) > MAX_CHARS:
            break
        per[r["meeting_id"]] = per.get(r["meeting_id"], 0) + 1
        chars += len(r["text"])
        out.append(r)
        if len(out) >= MAX_CHUNKS:
            break
    return out


def list_meetings(u: dict, folder: str, q: dict) -> list[dict]:
    w, args = _scope_sql(u, folder, q)
    if not (q.get("date_from") or q.get("date_to")):
        since = (_today()[0] - dt.timedelta(days=30)).isoformat()
        w += " AND m.start_time >= ?"; args.append(since)
    return db.q(f"SELECT m.id AS meeting_id, m.title, m.start_time, m.summary FROM meeting m WHERE {w} ORDER BY m.start_time DESC LIMIT 60", args)


def ask(u: dict, question: str, folder: str = "", history: list[dict] | None = None) -> dict:
    access.require(u)
    question = (question or "").strip()[:500]
    if not question:
        raise ValueError("请输入问题")
    if not llm.configured():
        raise ValueError("管理员还没有配置大模型")
    t0 = time.time()
    q = understand(question, history or [])
    sources = []
    if q["mode"] == "list":
        ms = list_meetings(u, folder, q)
        for i, m in enumerate(ms, 1):
            brief = re.sub(r"[#>*`\[\]]", "", (m["summary"] or ""))[:160].replace("\n", " ")
            sources.append({"n": i, "meeting_id": m["meeting_id"], "title": m["title"], "date": (m["start_time"] or "")[:16], "text": brief, "kind": "summary"})
    else:
        for i, r in enumerate(retrieve(u, question, folder, q), 1):
            src = {"n": i, "meeting_id": r["meeting_id"], "title": r["title"], "date": (r["start_time"] or "")[:16],
                   "text": r["text"].split("\n", 1)[-1], "kind": r["kind"]}
            if r["kind"] == "material":     # 会议材料：出处写成“《文件》第 n 页”
                src.update(meeting_id=None, material_id=-r["meeting_id"], page=r.get("seq"))
            sources.append(src)
    if not sources:
        answer = "在你能查看的会议记录里没有找到相关内容。可以换个说法、去掉时间范围，或者用顶部搜索框按关键词搜一下。"
    else:
        kname = lambda s: "会议材料 第 %s 页" % s.get("page") if s["kind"] == "material" else ("摘要" if s["kind"] == "summary" else "原文")  # noqa: E731
        material = "\n\n".join(f"[{s['n']}]《{s['title']}》{s['date']}（{kname(s)}）\n{s['text']}" for s in sources)
        hist = [m for h in (history or [])[-2:] for m in ({"role": "user", "content": h.get("q", "")[:300]}, {"role": "assistant", "content": h.get("a", "")[:600]})]
        answer = llm.chat([{"role": "system", "content": ANSWER}, *hist,
                           {"role": "user", "content": f"【会议资料】\n{material}\n\n【问题】{q['rewrite'] or question}"}],
                          max_tokens=1500, timeout=120, temperature=0.2, task="ask")
    used = {int(n) for n in re.findall(r"\[(\d{1,2})\]", answer)}
    # 前端只需要被引用的出处（列举模式全给）；资料原文截短
    shown = [dict(s, text=s["text"][:300], cited=(s["n"] in used)) for s in sources]
    secs = round(time.time() - t0, 1)
    scope = folder or ("全部" if u["see_all"] else rules.BIZ)
    with db.tx() as c:
        lid = db.insert(c, "ask_log", {"user_id": u["id"], "user_name": u["name"], "scope": scope, "question": question, "answer": answer,
                                       "sources": json.dumps([{"n": s["n"], "meeting_id": s["meeting_id"], "material_id": s.get("material_id"), "page": s.get("page"), "title": s["title"]} for s in shown], ensure_ascii=False),
                                       "seconds": secs, "created_at": access.now()})
    return {"id": lid, "answer": answer, "sources": shown, "keywords": q["keywords"], "mode": q["mode"], "seconds": secs,
            "range": [q["date_from"], q["date_to"]], "vector": embed.ready()}


def rate(u: dict, log_id: int, rating: int, comment: str = ""):
    row = db.q1("SELECT user_id FROM ask_log WHERE id = ?", (log_id,))
    if not row or (row["user_id"] != u["id"] and not u["is_admin"]):
        raise PermissionError("找不到这条提问")
    with db.tx() as c:
        c.execute("UPDATE ask_log SET rating = ?, comment = ? WHERE id = ?", (1 if rating > 0 else -1, (comment or "")[:500], log_id))
