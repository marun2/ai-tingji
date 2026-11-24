"""导出 Word：每场会一个 .docx（摘要 + 待办 + 原文在同一个文件），多场打包成 zip，保留「文件夹/月份」结构。只导出提问人有权限的会。"""
import io
import re
import zipfile

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from . import access, db

MAX_MEETINGS = 300


def _font(doc):
    st = doc.styles["Normal"]
    st.font.name = "微软雅黑"
    st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")


def _inline(p, text):
    """支持 **加粗**"""
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            p.add_run(part).bold = bool(i % 2)


def _md(doc, text):
    for line in (text or "").split("\n"):
        s = line.rstrip()
        if not s.strip():
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", s)
        if m:
            doc.add_heading(m[2].strip(), level=min(4, len(m[1]) + 1))
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)", s)
        if m:
            _inline(doc.add_paragraph(style="List Bullet"), m[1]); continue
        m = re.match(r"^\s*\d+[.、)]\s+(.*)", s)
        if m:
            _inline(doc.add_paragraph(style="List Number"), m[1]); continue
        m = re.match(r"^\s*>\s?(.*)", s)
        if m:
            p = doc.add_paragraph(); _inline(p, m[1])
            for r in p.runs:
                r.font.color.rgb = RGBColor(0x55, 0x5E, 0x6E)
            continue
        if re.match(r"^\s*\|.*\|\s*$", s):
            if re.match(r"^\s*\|[\s:|-]+\|\s*$", s):
                continue
            s = " ｜ ".join(c.strip() for c in s.strip().strip("|").split("|"))
        _inline(doc.add_paragraph(), s)


def meeting_docx(m: dict) -> bytes:
    doc = Document()
    _font(doc)
    doc.add_heading(m["title"], level=0)
    facts = [f"时间：{m.get('start_time') or '日期不详'}"]
    if m.get("creator"):
        facts.append(f"创建人：{m['creator']}")
    if m.get("duration"):
        facts.append(f"时长：{m['duration']} 分钟")
    facts.append(f"主体：{m.get('entity') or ''}")
    if m.get("folders"):
        facts.append("文件夹：" + "、".join(m["folders"]))
    p = doc.add_paragraph(" ｜ ".join(facts))
    for r in p.runs:
        r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
    doc.add_heading("摘要", level=1)
    if m.get("summary"):
        _md(doc, m["summary"])
    else:
        doc.add_paragraph("（无摘要）")
    todos = [t.strip() for t in re.split(r"[;；\n]", m.get("todos") or "") if t.strip()]
    if todos:
        doc.add_heading("待办", level=1)
        for t in todos:
            doc.add_paragraph(t, style="List Number")
    doc.add_heading("原文", level=1)
    if m.get("truncated"):
        doc.add_paragraph("（原文不完整：后半段在导出来源里已缺失）").runs[0].italic = True
    for line in (m.get("transcript") or "（无原文）").split("\n"):
        if not line.strip():
            continue
        mm = re.match(r"^\[([^\]]{1,20})\]\s*(.*)", line)
        p = doc.add_paragraph()
        if mm:
            p.add_run(mm[1] + "：").bold = True
            p.add_run(mm[2])
        else:
            p.add_run(line)
    foot = doc.add_paragraph("导出自 AI听记 · 公司内部资料，请勿外传")
    foot.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for r in foot.runs:
        r.font.size = Pt(8); r.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def safe(name: str, n: int = 60) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip(" ._")[:n] or "未命名"


def _load(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    ms = db.q(f"SELECT * FROM meeting WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY start_time", ids)
    fm = {}
    for r in db.q(f"SELECT meeting_id, folder FROM meeting_folder WHERE meeting_id IN ({','.join('?' for _ in ids)})", ids):
        fm.setdefault(r["meeting_id"], []).append(r["folder"])
    for m in ms:
        m["folders"] = fm.get(m["id"], [])
    return ms


def export(u: dict, ids: list[int] | None = None, folder: str = "", month: str = "") -> tuple[str, bytes, int]:
    """返回 (文件名, 内容, 会议数)。一场会直接给 .docx，多场给 .zip"""
    access.require(u)
    cond, params = access.meeting_filter(u)
    where, args = [cond], list(params)
    if ids:
        ids = [int(i) for i in ids][:MAX_MEETINGS]
        where.append(f"m.id IN ({','.join('?' for _ in ids)})"); args += ids
    if folder:
        if folder not in access.folders_for(u):
            raise PermissionError("没有这个文件夹的权限")
        where.append("EXISTS (SELECT 1 FROM meeting_folder f2 WHERE f2.meeting_id = m.id AND f2.folder = ?)"); args.append(folder)
    if month == "unknown":
        where.append("m.start_time IS NULL")
    elif month:
        where.append("substr(m.start_time, 1, 7) = ?"); args.append(month)
    if not (ids or folder):
        raise ValueError("请选择要导出的会议或文件夹")
    allowed = [r["id"] for r in db.q(f"SELECT m.id FROM meeting m WHERE {' AND '.join(where)} ORDER BY m.start_time DESC LIMIT {MAX_MEETINGS + 1}", args)]
    if not allowed:
        raise ValueError("没有可以导出的会议")
    if len(allowed) > MAX_MEETINGS:
        raise ValueError(f"一次最多导出 {MAX_MEETINGS} 场，请按月份分开导出")
    ms = _load(allowed)
    if len(ms) == 1:
        m = ms[0]
        return f"{(m['start_time'] or '')[:10]} {safe(m['title'])}.docx".strip(), meeting_docx(m), 1
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for m in ms:
            fdir = folder or (m["folders"][0] if m["folders"] else "未归类")
            mdir = (m["start_time"] or "")[:7] or "日期不详"
            name = f"{fdir}/{mdir}/{(m['start_time'] or '')[:10]} {safe(m['title'])}".replace("/ ", "/")
            n, k = name, 2
            while n in used:
                n = f"{name}（{k}）"; k += 1
            used.add(n)
            z.writestr(n + ".docx", meeting_docx(m))
    label = safe(folder or "选中的会议") + (f"-{month}" if month and month != "unknown" else "")
    return f"AI听记-{label}.zip", buf.getvalue(), len(ms)
