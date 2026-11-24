"""把会议切成检索用的小段：摘要按段落/小标题切，原文按发言行凑满约 600 字。
每段前面带上「标题 · 日期」，检索和大模型回答时都知道这段出自哪场会。"""
import re

from . import db

SIZE = 600


def _pack(units: list[str], size: int = SIZE) -> list[str]:
    out, buf = [], ""
    for u in units:
        u = u.strip()
        if not u:
            continue
        while len(u) > size * 1.5:          # 特别长的一行先切开
            out.append((buf + "\n" + u[:size]).strip() if buf else u[:size]); buf = ""
            u = u[size:]
        if len(buf) + len(u) > size and buf:
            out.append(buf); buf = u
        else:
            buf = (buf + "\n" + u) if buf else u
    if buf:
        out.append(buf)
    return out


def build(m: dict) -> list[tuple[str, str]]:
    head = f"【{m['title']}】{(m.get('start_time') or '')[:10]}"
    out = []
    summary = (m.get("summary") or "").strip()
    if summary:
        parts = re.split(r"\n(?=#{1,4}\s)", summary)      # 按小标题分节，节内再按长度凑
        for p in parts:
            for t in _pack(p.split("\n")):
                out.append(("summary", f"{head}（摘要）\n{t}"))
    todos = (m.get("todos") or "").strip()
    if todos:
        out.append(("summary", f"{head}（待办）\n{todos[:1200]}"))
    tr = (m.get("transcript") or "").strip()
    if tr:
        for t in _pack(tr.split("\n")):
            out.append(("transcript", f"{head}（原文）\n{t}"))
    return out


def rebuild(mids: list[int]):
    """重新切这些会议（新导入、内容更新）"""
    from . import embed
    for mid in mids:
        m = db.q1("SELECT id, title, start_time, summary, todos, transcript, status FROM meeting WHERE id = ?", (mid,))
        with db.tx() as c:
            c.execute("DELETE FROM chunk WHERE meeting_id = ?", (mid,))
            if m and m["status"] != "empty":
                c.executemany("INSERT INTO chunk (meeting_id, kind, seq, text) VALUES (?, ?, ?, ?)",
                              [(mid, k, i, t) for i, (k, t) in enumerate(build(m))])
    embed.forget_meetings(mids)


def backfill() -> int:
    """还没切过的会议补上（升级后第一次启动、或者以前导入的）"""
    rows = db.q("SELECT m.id FROM meeting m WHERE m.status <> 'empty' AND NOT EXISTS (SELECT 1 FROM chunk c WHERE c.meeting_id = m.id)")
    rebuild([r["id"] for r in rows])
    return len(rows)
