"""导入钉钉听记导出的 Excel，以及重新分类。

流程：读表 → 去掉没有标题/时间的行 → 只留 2026-08-14 以后、名单内主体 → 按听记链接去重
→ 合并「会议录制：xxx」和同名听记 → 标记空记录、原文截断 → 分类（规则 + 大模型）→ 入库
"""
import collections
import datetime as dt
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import openpyxl

from . import db, llm, rules

START_DATE = "2026-08-14"         # 只收这天以后的听记（用户要求）
EXCEL_CELL_MAX = 32767            # Excel 单元格上限，原文正好这么长说明被截断了
MERGE_MINUTES = 20                # 同名同创建人、开始时间相差 20 分钟内算同一场会
COLS = {"标题": "title", "创建人": "creator", "开始时间": "start_time", "时长(分钟)": "duration", "关键词": "keywords",
        "AI摘要": "summary", "待办事项": "todos", "转写原文": "transcript", "听记链接": "link", "所属组织": "org"}

_job_lock = threading.Lock()


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 文本清洗
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_OSS = re.compile(r"https?://[^\s)]*aliyuncs\.com[^\s)]*")
_ORIGIN = re.compile(r"\s*\{origin:[^}]*\}")  # 钉钉摘要里指向录音位置的标记


def _strs(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [s for x in v for s in _strs(x)]
    return []


def _block_md(b, depth: int, out: list[str]):
    """钉钉「图文纪要」里的块 → Markdown（只取文字，图标、样式都丢掉）"""
    if isinstance(b, list):
        for x in b:
            _block_md(x, depth, out)
        return
    if not isinstance(b, dict):
        if isinstance(b, str) and b.strip():
            out.append(b.strip())
        return
    title = b.get("title") or b.get("label") or b.get("name")
    content = _strs(b.get("content")) + _strs(b.get("text")) + _strs(b.get("value") if isinstance(b.get("value"), (str, list)) else None)
    kids = [b[k] for k in ("children", "items", "rows", "list", "blocks") if isinstance(b.get(k), list)]
    if b.get("type") == "module" and title:
        out.append(f"{'#' * min(4, depth)} {title}")
        title = None
    if title and content:
        out.append(f"- **{title}**：" + "；".join(content))
    elif title and kids:
        out.append(f"- **{title}**")
    elif title:
        out.append(f"- {title}")
    elif content:
        out.extend(("- " + c) if len(content) > 1 else c for c in content)
    for k in kids:
        _block_md(k, depth + 1, out)


def json_summary_to_md(s: str) -> str | None:
    """有些听记的 AI摘要 是钉钉图文纪要的 JSON，转成能读的 Markdown；不是这种格式就返回 None"""
    st = s.lstrip()
    if not st.startswith("{"):
        return None
    try:
        d = json.loads(st)
    except ValueError:
        return None
    if not isinstance(d, dict) or not ({"overview", "menu", "details", "meta_info"} & set(d)):
        return None
    meta = d.get("meta_info") or {}
    out = []
    if meta.get("title"):
        out.append(f"# {meta['title']}")
    if meta.get("subtitle"):
        out.append(f"> {meta['subtitle']}")
    if meta.get("tags"):
        out.append("【" + "】【".join(map(str, meta["tags"])) + "】")
    if isinstance(d.get("overview"), str):
        out += ["", d["overview"]]
    menu = {m.get("slice_id"): m for m in d.get("menu") or [] if isinstance(m, dict)}
    for det in d.get("details") or []:
        m = menu.get(det.get("slice_id"), {})
        out += ["", f"## {m.get('title') or (det.get('detail_json') or {}).get('tag') or '议题'}"]
        if m.get("summary"):
            out.append(f"> {m['summary']}")
        _block_md((det.get("detail_json") or {}).get("blocks") or [], 3, out)
    if not d.get("details"):
        for m in menu.values():
            out += [f"## {m.get('title', '')}", m.get("summary", "")]
    return "\n".join(out).strip()


def clean(text) -> str:
    """去掉摘要里的图片（带阿里云 OSS 长期签名链接，不能外传）和 HTML 标签"""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n")
    s = json_summary_to_md(s) or s
    s = _IMG.sub("", s)
    s = _TAG.sub("", s)
    s = _OSS.sub("", s)
    s = _ORIGIN.sub("", s)
    return s.strip()


def norm_title(t: str) -> str:
    t = re.sub(r"^\s*会议录制\s*[:：]\s*", "", t or "")
    return re.sub(r"\s+", "", t)


def link_id(link: str) -> str:
    m = re.search(r"/transcribes/([0-9A-Za-z_\-]+)", link or "")
    return m.group(1) if m else (link or "").strip()


def parse_time(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    s = str(v).strip().replace("/", "-")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T]?(\d{1,2})?:?(\d{2})?", s)
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h or 0):02d}:{int(mi or 0):02d}"


def _minutes(a: str, b: str) -> float:
    fa, fb = dt.datetime.strptime(a, "%Y-%m-%d %H:%M"), dt.datetime.strptime(b, "%Y-%m-%d %H:%M")
    return abs((fa - fb).total_seconds()) / 60


# ---------------------------------------------------------------- 读 Excel
def read_excel(path) -> list[dict]:
    wb = openpyxl.load_workbook(path)  # 不能用只读模式：听记链接在单元格的超链接里
    ws = next((w for w in wb.worksheets if w.max_row > 1), wb.active)
    header = [str(c.value or "").strip() for c in ws[1]]
    idx = {COLS[h]: i for i, h in enumerate(header) if h in COLS}
    missing = [h for h, k in COLS.items() if k not in idx and k in ("title", "start_time", "link")]
    if missing:
        raise ValueError(f"表格里找不到这些列：{'、'.join(missing)}。请上传钉钉 AI听记导出的原始 Excel")
    out = []
    for row in ws.iter_rows(min_row=2):
        rec = {}
        for k, i in idx.items():
            cell = row[i] if i < len(row) else None
            v = cell.value if cell is not None else None
            if k == "link":
                target = cell.hyperlink.target if (cell is not None and cell.hyperlink) else None
                v = target or (v if v and str(v).startswith("http") else None)
            rec[k] = v
        out.append(rec)
    return out


# ---------------------------------------------------------------- 分类
def classify_one(m: dict, use_llm: bool, biz_subs: list[str]) -> dict:
    r = rules.classify(m["org"] or "", m["title"], m.get("keywords") or "", m.get("summary") or "", biz_subs)
    if not r.get("entity"):
        return r
    out = {**r, "by": "rule"}
    if use_llm and r["entity"] == "上海食品" and not r["sensitive"]:
        try:
            g = llm.classify(m["title"], m.get("creator") or "", m.get("keywords") or "", m.get("summary") or (m.get("transcript") or "")[:3000], r["folders"])
            folders = g["folders"] or r["folders"]
            sensitive = r["sensitive"] or g["sensitive"]
            agree = (rules.BIZ in folders) == (rules.BIZ in r["folders"])
            conf = g["confidence"] if agree else min(g["confidence"], 0.5)
            out = {**r, "folders": folders, "type": g["type"] if r["type"] == "其他" else r["type"], "sensitive": sensitive,
                   "confidence": conf, "reason": "大模型：" + g["reason"] + ("" if agree else "（和规则判断不一致）"), "by": "llm"}
        except llm.LLMError as e:
            out["reason"] += f"（大模型失败：{str(e)[:60]}）"
    if out["sensitive"]:
        out["folders"] = [f for f in out["folders"] if f != rules.BIZ] or ["人事"]
    out["pending"] = (not out["folders"]) or out["confidence"] < 0.6
    return out


def biz_subs() -> list[str]:
    row = db.q1("SELECT v FROM setting WHERE k = 'biz_subs'")
    if row and row["v"]:
        try:
            return [s for s in json.loads(row["v"]) if s in rules.SUBS]
        except ValueError:
            pass
    return list(rules.DEFAULT_BIZ_SUBS)


def _apply(c, mid: int, res: dict):
    status = "pending" if res["pending"] else "ok"
    c.execute("UPDATE meeting SET entity = ?, mtype = ?, sensitive = ?, status = ?, confidence = ?, reason = ?, classified_by = ?, updated_at = ? "
              "WHERE id = ? AND status <> 'empty'",
              (res["entity"], res["type"], 1 if res["sensitive"] else 0, status, res["confidence"], res["reason"][:300], res["by"], now(), mid))
    c.execute("DELETE FROM meeting_folder WHERE meeting_id = ?", (mid,))
    c.executemany("INSERT INTO meeting_folder (meeting_id, folder) VALUES (?, ?)", [(mid, f) for f in dict.fromkeys(res["folders"])])


# ---------------------------------------------------------------- 导入
def start_import(batch_id: int, path: str):
    threading.Thread(target=_run_import, args=(batch_id, path), daemon=True).start()


def _progress(batch_id: int, step: str, done: int = 0, total: int = 0):
    with db.tx() as c:
        c.execute("UPDATE import_batch SET progress = ? WHERE id = ?", (json.dumps({"step": step, "done": done, "total": total}, ensure_ascii=False), batch_id))


def _run_import(batch_id: int, path: str):
    with _job_lock:
        try:
            stats = _do_import(batch_id, path)
            with db.tx() as c:
                c.execute("UPDATE import_batch SET status = 'done', stats = ?, finished_at = ? WHERE id = ?",
                          (json.dumps(stats, ensure_ascii=False), now(), batch_id))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            with db.tx() as c:
                c.execute("UPDATE import_batch SET status = 'failed', error = ?, finished_at = ? WHERE id = ?", (str(e)[:500], now(), batch_id))


def _do_import(batch_id: int, path: str) -> dict:
    _progress(batch_id, "读取表格")
    raw = read_excel(path)
    st = collections.Counter()
    skipped_orgs = collections.Counter()
    st["rows"] = len(raw)
    recs = []
    for r in raw:
        title = clean(r.get("title"))
        t = parse_time(r.get("start_time"))
        if not title or not t:
            st["no_title"] += 1
            continue
        if t[:10] < START_DATE:
            st["before_start"] += 1
            continue
        org = str(r.get("org") or "").strip()
        if not rules.entity_of(org):
            st["other_org"] += 1
            skipped_orgs[org or "（空）"] += 1
            continue
        tr = str(r.get("transcript") or "")
        try:
            dur = int(float(r.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        recs.append({"title": title, "creator": clean(r.get("creator")), "start_time": t, "duration": dur,
                     "keywords": clean(r.get("keywords")), "summary": clean(r.get("summary")), "todos": clean(r.get("todos")),
                     "transcript": tr.strip(), "truncated": 1 if len(tr) >= EXCEL_CELL_MAX else 0, "org": org,
                     "link": r.get("link") or "", "lid": link_id(r.get("link") or "") or f"{title}|{t}|{r.get('creator')}"})

    # 按链接去重（表内重复 + 库里已有）
    existing = {x["link_id"] for x in db.q("SELECT link_id FROM meeting_link")}
    seen, fresh = set(), []
    for r in recs:
        if r["lid"] in existing:
            st["dup_existing"] += 1
        elif r["lid"] in seen:
            st["dup_in_file"] += 1
        else:
            seen.add(r["lid"])
            fresh.append(r)

    # 合并同一场会（「会议录制：xxx」和听记）：先在表内合并，再和库里已有的比
    fresh.sort(key=lambda r: r["start_time"])
    groups: list[list[dict]] = []
    for r in fresh:
        key = (norm_title(r["title"]), r["creator"])
        for g in groups:
            h = g[0]
            if (norm_title(h["title"]), h["creator"]) == key and _minutes(h["start_time"], r["start_time"]) <= MERGE_MINUTES:
                g.append(r)
                break
        else:
            groups.append([r])

    def richness(r):
        return len(r["summary"]) * 3 + len(r["transcript"]) + (500 if not r["title"].startswith("会议录制") else 0)

    use_llm = llm.configured()
    subs = biz_subs()
    total = len(groups)
    touched: list[int] = []
    _progress(batch_id, "写入", 0, total)
    for gi, g in enumerate(groups):
        g.sort(key=richness, reverse=True)
        best = dict(g[0])
        # 内容取最全的那条，标题优先用不带「会议录制：」的
        best["title"] = next((r["title"] for r in g if not re.match(r"^\s*会议录制", r["title"])), best["title"])
        if len(g) > 1:
            st["merged"] += len(g) - 1
        # 库里有没有同一场会
        lo = (dt.datetime.strptime(best["start_time"], "%Y-%m-%d %H:%M") - dt.timedelta(minutes=MERGE_MINUTES)).strftime("%Y-%m-%d %H:%M")
        hi = (dt.datetime.strptime(best["start_time"], "%Y-%m-%d %H:%M") + dt.timedelta(minutes=MERGE_MINUTES)).strftime("%Y-%m-%d %H:%M")
        cands = db.q("SELECT id, title, summary, transcript, classified_by FROM meeting WHERE creator = ? AND start_time BETWEEN ? AND ?",
                     (best["creator"], lo, hi))
        same = next((c for c in cands if norm_title(c["title"]) == norm_title(best["title"])), None)
        empty = len(best["summary"]) < 30 and len(best["transcript"]) < 200
        with db.tx() as c:
            if same:
                mid = same["id"]
                st["merged"] += len(g)
                if richness(best) > len(same["summary"] or "") * 3 + len(same["transcript"] or ""):
                    c.execute("UPDATE meeting SET title = ?, summary = ?, todos = ?, transcript = ?, keywords = ?, truncated = ?, updated_at = ? WHERE id = ?",
                              (best["title"], best["summary"], best["todos"], best["transcript"], best["keywords"], best["truncated"], now(), mid))
                    if same["classified_by"] != "manual":
                        touched.append(mid)
            else:
                mid = db.insert(c, "meeting", {
                    "link": best["link"], "title": best["title"], "creator": best["creator"], "start_time": best["start_time"],
                    "duration": best["duration"], "keywords": best["keywords"], "summary": best["summary"], "todos": best["todos"],
                    "transcript": best["transcript"], "truncated": best["truncated"], "org": best["org"],
                    "entity": rules.entity_of(best["org"]), "status": "empty" if empty else "pending", "batch_id": batch_id,
                    "created_at": now(), "updated_at": now()})
                if empty:
                    st["empty"] += 1
                else:
                    st["new"] += 1
                    touched.append(mid)
                if best["truncated"]:
                    st["truncated"] += 1
            c.executemany("INSERT INTO meeting_link (link_id, meeting_id, title) VALUES (?, ?, ?)", [(r["lid"], mid, r["title"]) for r in g])
        if gi % 20 == 0:
            _progress(batch_id, "写入", gi + 1, total)

    st.update(_classify_ids(touched, use_llm, subs, lambda d, t: _progress(batch_id, "分类" + ("（大模型）" if use_llm else ""), d, t)))
    out = dict(st)
    out["skipped_orgs"] = dict(skipped_orgs)
    out["llm"] = use_llm
    return out


def _classify_ids(ids: list[int], use_llm: bool, subs: list[str], progress) -> dict:
    st = collections.Counter()
    if not ids:
        return st
    done = 0
    lock = threading.Lock()

    def work(mid):
        nonlocal done
        m = db.q1("SELECT * FROM meeting WHERE id = ?", (mid,))
        res = classify_one(m, use_llm, subs)
        with db.tx() as c:
            if res.get("entity"):
                _apply(c, mid, res)
        with lock:
            done += 1
            st["pending" if res.get("pending") else "classified"] += 1
            st["by_" + res.get("by", "rule")] += 1
            if done % 10 == 0 or done == len(ids):
                progress(done, len(ids))

    with ThreadPoolExecutor(max_workers=4 if use_llm else 1) as ex:
        list(ex.map(work, ids))
    return st


# ---------------------------------------------------------------- 重新分类（配好大模型以后用）
def reclassify_state() -> dict:
    row = db.q1("SELECT v FROM setting WHERE k = 'reclassify'")
    return json.loads(row["v"]) if row and row["v"] else {}


def _set_state(d: dict):
    v = json.dumps(d, ensure_ascii=False)
    with db.tx() as c:
        if db.IS_PG:
            c.execute("INSERT INTO setting (k, v) VALUES ('reclassify', ?) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (v,))
        else:
            c.execute("INSERT OR REPLACE INTO setting (k, v) VALUES ('reclassify', ?)", (v,))


def start_reclassify(by: str) -> dict:
    if _job_lock.locked():
        raise RuntimeError("正在导入或分类，请等它完成")
    ids = [r["id"] for r in db.q("SELECT id FROM meeting WHERE status <> 'empty' AND COALESCE(classified_by, '') <> 'manual' ORDER BY id")]
    _set_state({"running": True, "done": 0, "total": len(ids), "by": by, "started": now()})

    def run():
        with _job_lock:
            try:
                use = llm.configured()
                st = _classify_ids(ids, use, biz_subs(), lambda d, t: _set_state({"running": True, "done": d, "total": t, "by": by, "started": now()}))
                _set_state({"running": False, "done": len(ids), "total": len(ids), "by": by, "finished": now(), "stats": dict(st), "llm": use})
            except Exception as e:  # noqa: BLE001
                _set_state({"running": False, "error": str(e)[:300], "by": by, "finished": now()})

    threading.Thread(target=run, daemon=True).start()
    return {"total": len(ids)}
