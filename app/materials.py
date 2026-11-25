"""会议材料（PDF）。设计说明见 docs/会议PPT融合方案.md。

- 管理员上传 PDF（会议汇报材料，大多是 PPT 导出的，图片和文字混排）
- 后台逐页读文字：有文字层的直接读；图片占比大或文字很少的页，用本地 OCR（RapidOCR，onnxruntime，模型随安装包，不调大模型、不花钱）
- 自动匹配会议：文件名 / 首页里的日期 + 标题和内容的相似度，给出建议；管理员确认后才算数
- 挂到会议的材料跟着会议的权限走；不挂会议的材料由管理员放进某个文件夹
- 部门负责人只看得到“已确认、非敏感”的材料（和会议一样保守）
- 原文件留在 DATA_DIR/materials（持久目录）；页面图片按需渲染并缓存；管理员可以删除
- 检索：每页文字切成文本段写进 chunk 表，meeting_id 用“负的材料编号”，提问和向量检索时按权限放行
"""
import datetime as dt
import hashlib
import json
import re
import shutil
import threading
import time

from . import access, db, rules

DIR = db.DATA_DIR / "materials"
MAX_MB = 200
OCR_MIN_TEXT = 300          # 文字层少于这么多字的页做 OCR
OCR_IMAGE_RATIO = 0.25      # 图片面积占页面四分之一以上的页也做 OCR
_wake = threading.Event()
_state = {"running": False, "current": "", "ocr": None}
_ocr_engine = None
_ocr_lock = threading.Lock()
# PDFium（读 PDF 的库）不能多线程同时用，否则会让整个服务崩溃：所有调用都排队
_pdf_lock = threading.RLock()


def now():
    return access.now()


def _path(mid: int):
    return DIR / f"{mid}.pdf"


def _cache(mid: int):
    return DIR / f"{mid}.cache"


# ---------------------------------------------------------------- 上传
def save_upload(src_path: str, filename: str, by: str) -> dict:
    """把上传的临时文件存好，排队处理；同一个文件传过就跳过"""
    DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with open(src_path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    sha = h.hexdigest()
    old = db.q1("SELECT id, filename FROM material WHERE sha256 = ?", (sha,))
    if old:
        return {"id": old["id"], "duplicate": True, "filename": old["filename"]}
    import os
    size = os.path.getsize(src_path)
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I)
    with db.tx() as c:
        mid = db.insert(c, "material", {"filename": filename[:200], "sha256": sha, "size": size, "title": stem[:120],
                                        "doc_date": _date_from(stem), "status": "queued", "match_status": "pending",
                                        "sensitive": 1 if rules.SENSITIVE_TITLE.search(stem) or re.search(r"面试|候选人|简历", stem) else 0,
                                        "uploaded_by": by, "created_at": now(), "updated_at": now()})
    shutil.move(src_path, _path(mid))
    _wake.set()
    return {"id": mid, "duplicate": False, "filename": filename}


_DATE_PATS = [
    (r"(20\d{2})[-_.年/](\d{1,2})[-_.月/](\d{1,2})", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (r"(20\d{2})(\d{2})(\d{2})", lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (r"(?<!\d)(\d{1,2})[-_.月/](\d{1,2})日?(?!\d)", lambda m: (None, int(m[1]), int(m[2]))),
]


def _date_from(text: str) -> str | None:
    """从文件名或首页文字里找日期；只有月日的按最近的过去日期补年份"""
    today = dt.date.today()
    for pat, f in _DATE_PATS:
        for m in re.finditer(pat, text or ""):
            y, mo, d = f(m)
            try:
                if y is None:
                    cand = dt.date(today.year, mo, d)
                    if cand > today + dt.timedelta(days=7):
                        cand = dt.date(today.year - 1, mo, d)
                else:
                    cand = dt.date(y, mo, d)
                if dt.date(2024, 1, 1) <= cand <= today + dt.timedelta(days=60):
                    return cand.isoformat()
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------- 读 PDF
def _ocr():
    """懒加载 OCR 引擎；没装好就返回 None（只用文字层）"""
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                _ocr_engine = RapidOCR()
                _state["ocr"] = True
            except Exception as e:  # noqa: BLE001
                print("OCR 不可用，只读文字层：", repr(e)[:200], flush=True)
                _ocr_engine = False
                _state["ocr"] = False
        return _ocr_engine or None


def ocr_image(img) -> str:
    eng = _ocr()
    if not eng:
        return ""
    import numpy as np
    res, _t = eng(np.asarray(img.convert("RGB")))
    if not res:
        return ""
    # 按行排序：先上下、再左右
    items = sorted(((b[0][1], b[0][0], t) for b, t, s in res if s is None or float(s) >= 0.5), key=lambda x: (round(x[0] / 12), x[1]))
    lines, cur_y, buf = [], None, []
    for y, x, t in items:
        if cur_y is not None and abs(y - cur_y) > 12:
            lines.append(" ".join(buf)); buf = []
        buf.append(t); cur_y = y
    if buf:
        lines.append(" ".join(buf))
    return "\n".join(lines)


def _image_ratio(page) -> float:
    try:
        import pypdfium2.raw as pr
        w, h = page.get_size()
        area = 0.0
        for obj in page.get_objects(filter=[pr.FPDF_PAGEOBJ_IMAGE], max_depth=2):
            l, b, r, t = obj.get_pos()
            area += max(0, r - l) * max(0, t - b)
        return min(1.0, area / max(1.0, w * h))
    except Exception:  # noqa: BLE001
        return 0.0


def _clean(t: str) -> str:
    t = (t or "").replace("\r", "\n").replace("\x00", "")
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def process(mid: int):
    import pypdfium2 as pdfium
    m = db.q1("SELECT * FROM material WHERE id = ?", (mid,))
    if not m:
        return
    _state.update(current=m["filename"])
    with db.tx() as c:
        c.execute("UPDATE material SET status = 'processing', error = NULL, updated_at = ? WHERE id = ?", (now(), mid))
        c.execute("DELETE FROM material_page WHERE material_id = ?", (mid,))
    with _pdf_lock:
        pdf = pdfium.PdfDocument(str(_path(mid)))
        n = len(pdf)
    ocr_n = 0
    try:
        for i in range(n):
            with _pdf_lock:
                page = pdf[i]
                tp = page.get_textpage()
                text = _clean(tp.get_text_range())
                tp.close()
                need_ocr = len(text) < OCR_MIN_TEXT or _image_ratio(page) >= OCR_IMAGE_RATIO
                img = page.render(scale=2).to_pil() if need_ocr and _ocr() else None
                page.close()
            source = "text"
            if need_ocr:
                if img is not None:
                    o = _clean(ocr_image(img))
                    ocr_n += 1
                    if o:
                        have = set(text.split("\n"))
                        extra = "\n".join(x for x in o.split("\n") if x not in have)
                        text = (text + "\n" + extra).strip() if text else o
                        source = "ocr" if len(o) > len(text) * 0.6 else "text+ocr"
                elif not text:
                    source = "image"
            with db.tx() as c:
                c.execute("INSERT INTO material_page (material_id, page_no, text, source) VALUES (?, ?, ?, ?)", (mid, i + 1, text, source))
                c.execute("UPDATE material SET pages = ?, progress = ?, updated_at = ? WHERE id = ?", (n, i + 1, now(), mid))
    finally:
        with _pdf_lock:
            pdf.close()
    pages = db.q("SELECT page_no, text FROM material_page WHERE material_id = ? ORDER BY page_no", (mid,))
    first = next((p["text"] for p in pages if p["text"]), "")
    title = m["title"]
    if not m["doc_date"]:
        d = _date_from(first[:300])
        if d:
            with db.tx() as c:
                c.execute("UPDATE material SET doc_date = ? WHERE id = ?", (d, mid))
    sugg = suggest(mid)
    upd = {"suggestions": json.dumps(sugg, ensure_ascii=False), "ocr_pages": ocr_n, "status": "ok"}
    if m["match_status"] == "pending" and not m["meeting_id"] and not m["folder"]:
        if sugg and sugg[0]["score"] >= 0.35 and (len(sugg) < 2 or sugg[0]["score"] - sugg[1]["score"] >= 0.06):
            upd["meeting_id"] = sugg[0]["id"]
        else:
            upd["folder"] = _guess_folder(title, "\n".join(p["text"] for p in pages)[:6000])
    with db.tx() as c:
        sets = ", ".join(f"{k} = ?" for k in upd)
        c.execute(f"UPDATE material SET {sets}, updated_at = ? WHERE id = ?", (*upd.values(), now(), mid))
    rebuild_chunks(mid)


def _guess_folder(title: str, text: str) -> str:
    sc = rules.score(title, "", text)
    hits = sorted(((v, k) for k, v in sc.items() if v >= 3.5), reverse=True)
    if hits and hits[0][1] in ("人事", "财务", "IT", "董秘办") and hits[0][0] >= 6:
        return hits[0][1]
    return rules.BIZ


# ---------------------------------------------------------------- 匹配会议
_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?（）()“”\"'《》【】\[\]—\-_·…|/\\0-9a-zA-Z]+")


def _grams(s: str) -> set:
    s = _PUNCT.sub("", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _sim(a: set, b: set) -> float:
    return len(a & b) / (len(a | b) or 1)


def _cover(a: set, b: set) -> float:
    """a 有多少落在 b 里（材料标题短，用覆盖率比交并比更合适）"""
    return len(a & b) / (len(a) or 1)


def suggest(mid: int, top: int = 4) -> list[dict]:
    m = db.q1("SELECT * FROM material WHERE id = ?", (mid,))
    pages = db.q("SELECT text FROM material_page WHERE material_id = ? ORDER BY page_no LIMIT 6", (mid,))
    head = "\n".join(p["text"] for p in pages)[:3000]
    stem = re.sub(r"\.pdf$", "", m["filename"], flags=re.I)
    title_g = _grams(stem) | _grams(head.split("\n", 1)[0][:60])
    body_g = _grams(head)
    d = m["doc_date"]
    if d:
        lo = (dt.date.fromisoformat(d) - dt.timedelta(days=5)).isoformat()
        hi = (dt.date.fromisoformat(d) + dt.timedelta(days=5)).isoformat() + " 23:59"
        cands = db.q("SELECT id, title, start_time, summary FROM meeting WHERE status <> 'empty' AND start_time >= ? AND start_time <= ?", (lo, hi))
    else:
        since = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        cands = db.q("SELECT id, title, start_time, summary FROM meeting WHERE status <> 'empty' AND start_time >= ? ORDER BY start_time DESC LIMIT 1500", (since,))
    out = []
    for c in cands:
        mt = _grams(re.sub(r"^\d{1,2}[-./]\d{1,2}\s*", "", c["title"] or ""))
        s_title = max(_cover(mt, title_g), _sim(title_g, mt))
        s_body = _cover(body_g, _grams((c["summary"] or "")[:3000])) if body_g else 0
        s_date = 0.0
        if d and c["start_time"]:
            gap = abs((dt.date.fromisoformat(c["start_time"][:10]) - dt.date.fromisoformat(d)).days)
            s_date = max(0.0, 1 - gap / 5)
        score = 0.45 * s_title + 0.35 * s_body + 0.2 * s_date
        out.append({"id": c["id"], "title": c["title"], "date": (c["start_time"] or "")[:10], "score": round(score, 3)})
    out.sort(key=lambda x: -x["score"])
    return [x for x in out[:top] if x["score"] >= 0.08]


# ---------------------------------------------------------------- 检索用的文本段
def rebuild_chunks(mid: int):
    from . import embed
    pages = db.q("SELECT page_no, text FROM material_page WHERE material_id = ? ORDER BY page_no", (mid,))
    m = db.q1("SELECT filename FROM material WHERE id = ?", (mid,))
    with db.tx() as c:
        c.execute("DELETE FROM chunk WHERE meeting_id = ?", (-mid,))
        for p in pages:
            t = (p["text"] or "").strip()
            if len(t) < 8:
                continue
            for k in range(0, len(t), 700):
                c.execute("INSERT INTO chunk (meeting_id, kind, seq, text) VALUES (?, 'material', ?, ?)",
                          (-mid, p["page_no"], f"《{m['filename']}》第 {p['page_no']} 页\n{t[k:k + 700]}"))
    embed.kick()


# ---------------------------------------------------------------- 后台
def _worker():
    while True:
        try:
            todo = db.q("SELECT id FROM material WHERE status IN ('queued', 'processing') ORDER BY id")
            for r in todo:
                _state.update(running=True)
                try:
                    process(r["id"])
                except Exception as e:  # noqa: BLE001
                    print("材料处理失败：", r["id"], repr(e), flush=True)
                    with db.tx() as c:
                        c.execute("UPDATE material SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (str(e)[:300], now(), r["id"]))
            _state.update(running=False, current="")
        except Exception as e:  # noqa: BLE001
            print("材料后台出错：", e, flush=True)
        _wake.wait(timeout=300)
        _wake.clear()


_started = False


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_worker, daemon=True).start()


def state() -> dict:
    return dict(_state)


# ---------------------------------------------------------------- 权限
def _visible_sql(u: dict, alias: str = "mt") -> tuple[str, list]:
    """材料能不能看：管理员 / 董事局全部；其他人只看已确认、非敏感、（挂的会能看 或 放在自己能看的文件夹）"""
    access.require(u)
    if u["see_all"]:
        return "1 = 1", []
    cond, params = access.meeting_filter(u, "mm")
    folders = access.folders_for(u)
    if not folders:
        return "1 = 0", []
    ph = ",".join("?" for _ in folders)
    return (f"({alias}.match_status = 'confirmed' AND {alias}.sensitive = 0 AND {alias}.status = 'ok' AND ("
            f"({alias}.meeting_id IS NOT NULL AND EXISTS (SELECT 1 FROM meeting mm WHERE mm.id = {alias}.meeting_id AND {cond})) OR "
            f"({alias}.meeting_id IS NULL AND {alias}.folder IN ({ph}))))"), [*params, *folders]


def visible_ids(u: dict, folder: str = "", date_from: str = "", date_to: str = "") -> list[int]:
    w, a = _visible_sql(u)
    where, args = [w, "mt.status = 'ok'"], list(a)
    if folder:
        where.append("(mt.folder = ? OR EXISTS (SELECT 1 FROM meeting_folder f2 WHERE f2.meeting_id = mt.meeting_id AND f2.folder = ?))")
        args += [folder, folder]
    eff = "COALESCE((SELECT substr(m2.start_time, 1, 10) FROM meeting m2 WHERE m2.id = mt.meeting_id), mt.doc_date, substr(mt.created_at, 1, 10))"
    if date_from:
        where.append(f"{eff} >= ?"); args.append(date_from)
    if date_to:
        where.append(f"{eff} <= ?"); args.append(date_to)
    return [r["id"] for r in db.q(f"SELECT mt.id FROM material mt WHERE {' AND '.join(where)}", args)]


def can_view(u: dict, mid: int) -> bool:
    w, a = _visible_sql(u)
    return bool(db.q1(f"SELECT 1 FROM material mt WHERE mt.id = ? AND {w}", (mid, *a)))


COLS = ("mt.id, mt.filename, mt.title, mt.size, mt.pages, mt.doc_date, mt.meeting_id, mt.folder, mt.sensitive, mt.match_status, "
        "mt.status, mt.progress, mt.error, mt.ocr_pages, mt.uploaded_by, mt.created_at, mt.suggestions")


def _decorate(rows: list[dict]) -> list[dict]:
    mids = [r["meeting_id"] for r in rows if r.get("meeting_id")]
    ms = {}
    if mids:
        for m in db.q(f"SELECT id, title, start_time FROM meeting WHERE id IN ({','.join('?' for _ in mids)})", mids):
            ms[m["id"]] = m
    for r in rows:
        m = ms.get(r.get("meeting_id"))
        r["meeting"] = {"id": m["id"], "title": m["title"], "date": (m["start_time"] or "")[:10]} if m else None
        r["date"] = (m["start_time"][:10] if m and m["start_time"] else None) or r.get("doc_date") or (r.get("created_at") or "")[:10]
        if "suggestions" in r:
            r["suggestions"] = json.loads(r["suggestions"] or "[]")
    return rows


def for_meeting(u: dict, meeting_id: int) -> list[dict]:
    w, a = _visible_sql(u)
    return _decorate(db.q(f"SELECT {COLS} FROM material mt WHERE mt.meeting_id = ? AND {w} ORDER BY mt.id", (meeting_id, *a)))


def list_visible(u: dict, q: str = "") -> list[dict]:
    """会议档案里的「会议材料」：能看的全部材料，最新的在前"""
    w, a = _visible_sql(u)
    where, args = [w], list(a)
    if q:
        where.append("(mt.filename LIKE ? OR EXISTS (SELECT 1 FROM material_page p WHERE p.material_id = mt.id AND p.text LIKE ?))")
        args += [f"%{q}%", f"%{q}%"]
    rows = db.q(f"SELECT {COLS} FROM material mt WHERE {' AND '.join(where)} ORDER BY mt.id DESC LIMIT 300", args)
    out = _decorate(rows)
    if not u["see_all"]:
        for r in out:
            r.pop("suggestions", None)
    return out


def get(u: dict, mid: int) -> dict:
    if not can_view(u, mid):
        raise PermissionError("找不到这份材料")
    r = _decorate(db.q(f"SELECT {COLS} FROM material mt WHERE mt.id = ?", (mid,)))[0]
    r["page_list"] = db.q("SELECT page_no, text, source FROM material_page WHERE material_id = ? ORDER BY page_no", (mid,))
    if not u["see_all"]:
        r.pop("suggestions", None)
    return r


def search_hits(u: dict, groups: list[list[str]]) -> list[dict]:
    """顶部搜索：材料里的命中（每份材料给出命中的页）"""
    ids = visible_ids(u)
    if not ids or not groups:
        return []
    where, args = [f"p.material_id IN ({','.join('?' for _ in ids)})"], list(ids)
    for g in groups:
        where.append("(" + " OR ".join("(p.text LIKE ?)" for _ in g) + ")")
        args += [f"%{t}%" for t in g]
    rows = db.q(f"SELECT p.material_id, p.page_no, p.text FROM material_page p WHERE {' AND '.join(where)} ORDER BY p.material_id DESC, p.page_no LIMIT 200", args)
    by = {}
    for r in rows:
        by.setdefault(r["material_id"], []).append(r)
    mats = {m["id"]: m for m in _decorate(db.q(f"SELECT {COLS} FROM material mt WHERE mt.id IN ({','.join('?' for _ in by)})", list(by)))} if by else {}
    flat = [t for g in groups for t in g]
    out = []
    for mid, ps in by.items():
        m = mats.get(mid)
        if not m:
            continue
        t = ps[0]["text"]
        i = min((t.find(x) for x in flat if x in t), default=0)
        out.append({"id": mid, "filename": m["filename"], "date": m["date"], "meeting": m["meeting"], "folder": m["folder"],
                    "pages": [p["page_no"] for p in ps][:12], "snippet": t[max(0, i - 40): i + 80].replace("\n", " ")})
    return out[:30]


# ---------------------------------------------------------------- 页面图片（按需渲染并缓存）
def page_image(u: dict, mid: int, page_no: int, width: int) -> bytes:
    if not can_view(u, mid):
        raise PermissionError("找不到这份材料")
    width = 320 if width <= 400 else 1400
    cdir = _cache(mid)
    cdir.mkdir(parents=True, exist_ok=True)
    f = cdir / f"p{page_no}-{width}.jpg"
    if f.exists():
        return f.read_bytes()
    import pypdfium2 as pdfium
    with _pdf_lock:
        pdf = pdfium.PdfDocument(str(_path(mid)))
        try:
            if not 1 <= page_no <= len(pdf):
                raise PermissionError("没有这一页")
            page = pdf[page_no - 1]
            w, _h = page.get_size()
            img = page.render(scale=width / max(1.0, w)).to_pil().convert("RGB")
            page.close()
        finally:
            pdf.close()
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=78 if width > 400 else 70, optimize=True)
    f.write_bytes(buf.getvalue())
    return buf.getvalue()


def original(u: dict, mid: int) -> tuple[str, bytes]:
    if not can_view(u, mid):
        raise PermissionError("找不到这份材料")
    m = db.q1("SELECT filename FROM material WHERE id = ?", (mid,))
    return m["filename"], _path(mid).read_bytes()


# ---------------------------------------------------------------- 管理
def admin_list(status: str = "") -> dict:
    where = {"review": "mt.match_status = 'pending' AND mt.status = 'ok'", "processing": "mt.status IN ('queued', 'processing')",
             "failed": "mt.status = 'failed'"}.get(status, "1 = 1")
    rows = _decorate(db.q(f"SELECT {COLS} FROM material mt WHERE {where} ORDER BY mt.id DESC LIMIT 500"))
    cnt = db.q1("SELECT COUNT(*) AS n, SUM(CASE WHEN match_status = 'pending' AND status = 'ok' THEN 1 ELSE 0 END) AS review, "
                "SUM(CASE WHEN status IN ('queued', 'processing') THEN 1 ELSE 0 END) AS processing, "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, SUM(size) AS bytes FROM material")
    return {"items": rows, "counts": {k: int(cnt[k] or 0) for k in ("n", "review", "processing", "failed", "bytes")}, "state": state()}


def update(mid: int, d: dict) -> dict:
    m = db.q1("SELECT * FROM material WHERE id = ?", (mid,))
    if not m:
        raise ValueError("找不到这份材料")
    upd = {}
    if "meeting_id" in d:
        if d["meeting_id"]:
            if not db.q1("SELECT 1 FROM meeting WHERE id = ?", (int(d["meeting_id"]),)):
                raise ValueError("找不到这场会")
            upd["meeting_id"] = int(d["meeting_id"])
            upd["folder"] = None
        else:
            upd["meeting_id"] = None
            f = d.get("folder") or m["folder"] or rules.BIZ
            if f not in rules.FOLDERS:
                raise ValueError("文件夹不对")
            upd["folder"] = f
    elif d.get("folder"):
        if d["folder"] not in rules.FOLDERS:
            raise ValueError("文件夹不对")
        upd["folder"] = d["folder"]; upd["meeting_id"] = None
    if "sensitive" in d:
        upd["sensitive"] = 1 if d["sensitive"] else 0
    if d.get("confirm"):
        upd["match_status"] = "confirmed"
    if d.get("unconfirm"):
        upd["match_status"] = "pending"
    if not upd:
        return m
    with db.tx() as c:
        sets = ", ".join(f"{k} = ?" for k in upd)
        c.execute(f"UPDATE material SET {sets}, updated_at = ? WHERE id = ?", (*upd.values(), now(), mid))
    return db.q1("SELECT * FROM material WHERE id = ?", (mid,))


def confirm_many(ids: list[int]) -> int:
    ids = [int(i) for i in ids][:500]
    if not ids:
        return 0
    with db.tx() as c:
        c.execute(f"UPDATE material SET match_status = 'confirmed', updated_at = ? WHERE status = 'ok' AND id IN ({','.join('?' for _ in ids)})", (now(), *ids))
    return len(ids)


def delete(mid: int):
    with db.tx() as c:
        c.execute("DELETE FROM chunk WHERE meeting_id = ?", (-mid,))
        c.execute("DELETE FROM material_page WHERE material_id = ?", (mid,))
        c.execute("DELETE FROM material WHERE id = ?", (mid,))
    _path(mid).unlink(missing_ok=True)
    shutil.rmtree(_cache(mid), ignore_errors=True)
    from . import embed
    embed.kick()


def retry(mid: int):
    with db.tx() as c:
        c.execute("UPDATE material SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?", (now(), mid))
    _wake.set()


def wait_idle(timeout: float = 60):
    """冒烟测试用：等后台处理完"""
    t = time.time()
    while time.time() - t < timeout:
        if not db.q1("SELECT 1 FROM material WHERE status IN ('queued', 'processing')"):
            return True
        time.sleep(0.2)
    return False
