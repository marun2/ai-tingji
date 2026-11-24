"""把管理员手工整理的历史听记压缩包（按部门分好的 Word 文件夹）转成平台能导入的「历史听记包」。

用法：python scripts/history_pack.py 均瑶健康文件_全部门.zip 历史听记包.json.gz

- 压缩包里是 <部门文件夹>/<主体>-<标题>-原文.docx 和 -摘要.docx；Word 里嵌了大量图片，原包很大，这里只留文字
- 文件夹 = 管理员手工分的归属，导入后当作“人工确认”，大模型重新分类也不会改
- 只收 2026-08-14 以前的（以后的以钉钉导出的 Excel 为准）；日期不详的也收
- 生成的包含真实会议内容，不要提交进仓库
"""
import collections
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import re
import sys
import zipfile

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parent.parent.as_posix())
from app import rules  # noqa: E402

CUTOFF = "2026-08-14"
FOLDER_MAP = {"研发-市场-销售-公司经营": rules.BIZ, "IT部门": "IT", "润盈": "均瑶润盈", "人事": "人事", "财务": "财务", "董秘办": "董秘办",
              "每日博士": "每日博士", "大健康": "大健康", "均瑶云商": "均瑶云商", "衢州食品": "衢州食品", "均瑶宜昌": "均瑶宜昌", "泛缘": "泛缘"}
PREFIX_ENTITY = {"上海食品": "上海食品", "每日博士": "每日博士", "大健康": "大健康", "均瑶云商": "均瑶云商", "均瑶润盈": "均瑶润盈",
                 "衢州食品": "衢州食品", "均瑶宜昌": "均瑶宜昌", "泛缘": "泛缘", "测试部门": "测试部门"}


def zname(info) -> str:
    n = info.filename
    if info.flag_bits & 0x800 == 0:
        try:
            n = n.encode("cp437").decode("utf-8")
        except UnicodeError:
            n = n.encode("cp437").decode("gbk", "replace")
    return n


def docx_text(data: bytes) -> str:
    try:
        x = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
    x = re.sub(r"<w:tab/>", "\t", x)
    x = re.sub(r"<w:br/>", "\n", x)
    x = re.sub(r"</w:p>", "\n", x)
    t = html.unescape(re.sub(r"<[^>]+>", "", x))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def guess_year(month: int) -> int:
    return 2025 if month >= 9 else 2026  # 历史包覆盖 2025 年下半年到 2026 年 8 月


def find_time(title: str, text: str) -> str | None:
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)[ T](\d\d):(\d\d)", text[:800])
    if m:
        return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}"
    m = re.search(r"(20\d\d)[-年](\d{1,2})[-月](\d{1,2})日?(?:\s+(\d{1,2})[_:](\d{2}))?", title)
    if m:
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d} {int(m[4] or 0):02d}:{int(m[5] or 0):02d}"
    m = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[_:](\d{2})", title)
    if m:
        mo = int(m[1])
        return f"{guess_year(mo)}-{mo:02d}-{int(m[2]):02d} {int(m[3]):02d}:{m[4]}"
    m = re.match(r"(\d{2})-(\d{2})\s", title)
    if m:
        mo = int(m[1])
        return f"{guess_year(mo)}-{mo:02d}-{m[2]} 00:00"
    m = re.search(r"_(\d{2})(\d{2})(?:_(\d{2})_(\d{2}))?(?:\D|$)", title)
    if m and 1 <= int(m[1]) <= 12 and 1 <= int(m[2]) <= 31:
        mo = int(m[1])
        return f"{guess_year(mo)}-{mo:02d}-{m[2]} {int(m[3] or 0):02d}:{int(m[4] or 0):02d}"
    return None


def norm(t: str) -> str:
    t = re.sub(r"^\d+_", "", t)
    t = re.sub(r"_1$", "", t)
    t = re.sub(r"^\s*会议录制\s*[:：]\s*", "", t)
    return re.sub(r"\s+", "", t)


def main(src, dst):
    z = zipfile.ZipFile(src)
    items: dict[tuple, dict] = {}
    for info in z.infolist():
        n = zname(info)
        if not n.lower().endswith(".docx"):
            continue
        parts = n.split("/")
        folder = FOLDER_MAP.get(parts[-2]) if len(parts) >= 2 else None
        if not folder:
            continue
        stem = parts[-1][:-5]
        m = re.match(r"(.*)-(原文|摘要)$", stem)
        base, kind = (m[1], m[2]) if m else (stem, "原文")
        prefix = base.split("-", 1)[0]
        if prefix in PREFIX_ENTITY:
            entity, title = PREFIX_ENTITY[prefix], base[len(prefix) + 1:]
        else:  # 没有主体前缀的（如 03_03-30 内部会议…）：子公司文件夹归子公司，其余归上海食品
            entity, title = (folder if folder in rules.SUBS else "上海食品"), base
        key = (entity, norm(title))
        it = items.setdefault(key, {"entity": entity, "title": re.sub(r"_1$", "", re.sub(r"^\d+_", "", title)).strip(),
                                    "folders": set(), "summary": "", "transcript": "", "files": set()})
        it["folders"].add(folder)
        it["files"].add(parts[-1])
        text = docx_text(z.read(info))
        field = "summary" if kind == "摘要" else "transcript"
        if len(text) > len(it[field]):
            it[field] = text
    out, st = [], collections.Counter()
    for (entity, nt), it in items.items():
        t = find_time(it["title"], it["summary"] or it["transcript"])
        if t and t[:10] >= CUTOFF:
            st["after_cutoff"] += 1
            continue
        st["no_date" if not t else "dated"] += 1
        hid = "hist:" + hashlib.sha1(f"{entity}|{nt}|{t or ''}".encode()).hexdigest()[:20]
        out.append({"id": hid, "title": it["title"], "start_time": t, "entity": entity, "folders": sorted(it["folders"]),
                    "summary": it["summary"], "transcript": it["transcript"], "files": sorted(it["files"])})
    out.sort(key=lambda r: r["start_time"] or "")
    data = {"kind": "tingji-history", "version": 1, "created": dt.datetime.now().isoformat(timespec="seconds"), "items": out}
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"会议 {len(out)} 场（有日期 {st['dated']}，日期不详 {st['no_date']}）；8月14日以后的 {st['after_cutoff']} 场跳过（以 Excel 为准）")
    print("文件夹分布：", collections.Counter(f for r in out for f in r["folders"]).most_common())


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
