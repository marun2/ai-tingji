"""从会议摘要里抽结构化信息（大模型）：决议、风险/问题、关键数字、客户/产品/竞品/项目/区域、话题。
周报月报和可视化都用这里的结果。只抽到“部门/话题/客户/产品”这一层，不抽人名（用户要求）。

- 导入后自动对新会议抽取；管理页可以手动“补全抽取”（只抽还没抽过的）或“全部重抽”
- 每场会抽完写 meeting.extracted_at；内容更新（重新导入更全的版本）时清空，下次补上
"""
import collections
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from . import access, db, llm

ENTITY_TYPES = ["客户", "产品", "竞品", "项目", "区域"]
_lock = threading.Lock()

PROMPT = """你在整理均瑶健康（食品饮料公司）的会议记录。从下面这场会的摘要里提取信息，只输出 JSON：
{"decisions": ["会上做出的决定/定下的事，每条一句话，最多 6 条"],
 "risks": ["暴露出的问题、风险、困难，每条一句话，最多 5 条"],
 "metrics": [{"name": "指标名，如 销售额/回款/利润/产能/铺市率/库存/费用/目标", "value": "数值，照原文", "unit": "单位", "context": "一句话说明是谁/哪里/什么时间的"}],
 "entities": {"客户": ["渠道或客户名，如 盒马、叮咚、7-Eleven、某经销商"], "产品": ["公司自己的产品/品类，如 营养瓶、椰子水、益生菌"],
              "竞品": ["竞争对手品牌，如 李子园"], "项目": ["项目或专项名，如 CNY、锤石项目"], "区域": ["地区/大区，如 浙江、东北大区、西南"]},
 "topics": ["3～5 个概括这场会主题的短标签，如 春节备货、渠道费用、新品上市"]}
规则：
- 只写摘要里明确出现的，不要推测；没有就给空数组
- 不要写人名（客户里也不要写个人名字，只写公司/渠道/品牌）
- 名称用最常见的简称，同一个东西用同一个写法（如 “盒马鲜生”写成“盒马”）
- metrics 最多 8 条，只要有具体数字的"""


def _norm(name: str) -> str:
    n = re.sub(r"[\s“”\"'《》【】()（）]", "", str(name or ""))
    n = re.sub(r"(有限公司|公司|集团|超市|鲜生|品牌)$", "", n)
    return n[:30]


def extract_one(m: dict) -> dict:
    content = f"会议：{m['title']}（{(m.get('start_time') or '')[:10]}）\n\n摘要：\n{(m.get('summary') or '')[:4500]}"
    if m.get("todos"):
        content += f"\n\n待办：\n{m['todos'][:800]}"
    if len(m.get("summary") or "") < 200 and m.get("transcript"):
        content += f"\n\n原文节选：\n{m['transcript'][:3000]}"
    s = llm.settings()
    text = llm.chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": content}],
                    model=s["llm_model_classify"] or None, max_tokens=1500, timeout=120)
    return llm.parse_json(text)


def _save(mid: int, d: dict):
    rows = []
    for t in (d.get("decisions") or [])[:8]:
        if str(t).strip():
            rows.append((mid, "decision", "", str(t).strip()[:300], "", ""))
    for t in (d.get("risks") or [])[:6]:
        if str(t).strip():
            rows.append((mid, "risk", "", str(t).strip()[:300], "", ""))
    for x in (d.get("metrics") or [])[:10]:
        if isinstance(x, dict) and str(x.get("value", "")).strip():
            rows.append((mid, "metric", str(x.get("name") or "")[:30], str(x.get("context") or "")[:200], str(x.get("value"))[:40], str(x.get("unit") or "")[:20]))
    ents = d.get("entities") or {}
    for typ in ENTITY_TYPES:
        seen = set()
        for e in (ents.get(typ) or [])[:12]:
            n = _norm(e)
            if len(n) >= 2 and n not in seen:
                seen.add(n)
                rows.append((mid, "entity", typ, n, "", ""))
    seen = set()
    for t in (d.get("topics") or [])[:6]:
        n = _norm(t)
        if len(n) >= 2 and n not in seen:
            seen.add(n)
            rows.append((mid, "topic", "", n, "", ""))
    with db.tx() as c:
        c.execute("DELETE FROM fact WHERE meeting_id = ?", (mid,))
        c.executemany("INSERT INTO fact (meeting_id, kind, label, text, value, unit) VALUES (?, ?, ?, ?, ?, ?)", rows)
        c.execute("UPDATE meeting SET extracted_at = ? WHERE id = ?", (access.now(), mid))


# ---------------------------------------------------------------- 后台任务
def state() -> dict:
    row = db.q1("SELECT v FROM setting WHERE k = 'extract'")
    st = json.loads(row["v"]) if row and row["v"] else {}
    total = db.q1("SELECT COUNT(*) AS n FROM meeting WHERE status <> 'empty'")["n"]
    done = db.q1("SELECT COUNT(*) AS n FROM meeting WHERE status <> 'empty' AND extracted_at IS NOT NULL")["n"]
    return {**st, "all": total, "extracted": done}


def _set(d: dict):
    v = json.dumps(d, ensure_ascii=False)
    with db.tx() as c:
        if db.IS_PG:
            c.execute("INSERT INTO setting (k, v) VALUES ('extract', ?) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (v,))
        else:
            c.execute("INSERT OR REPLACE INTO setting (k, v) VALUES ('extract', ?)", (v,))


def start(by: str, redo: bool = False) -> dict:
    if not llm.configured():
        raise RuntimeError("还没有配置大模型")
    if _lock.locked():
        raise RuntimeError("正在抽取，请等它完成")
    if redo:
        with db.tx() as c:
            c.execute("UPDATE meeting SET extracted_at = NULL")
    ids = [r["id"] for r in db.q("SELECT id FROM meeting WHERE status <> 'empty' AND extracted_at IS NULL ORDER BY start_time DESC")]
    threading.Thread(target=_run, args=(ids, by), daemon=True).start()
    return {"total": len(ids)}


def _run(ids: list[int], by: str):
    with _lock:
        st = collections.Counter()
        done = 0
        _set({"running": True, "done": 0, "total": len(ids), "by": by, "started": access.now()})
        lk = threading.Lock()

        def work(mid):
            nonlocal done
            m = db.q1("SELECT id, title, start_time, summary, todos, transcript FROM meeting WHERE id = ?", (mid,))
            try:
                if m and ((m["summary"] or "").strip() or (m["transcript"] or "").strip()):
                    _save(mid, extract_one(m))
                    st["ok"] += 1
                else:
                    st["skip"] += 1
            except Exception as e:  # noqa: BLE001
                st["fail"] += 1
                st["last_error"] = 0
                print(f"抽取失败 #{mid}：{e}", flush=True)
            with lk:
                done += 1
                if done % 10 == 0 or done == len(ids):
                    _set({"running": True, "done": done, "total": len(ids), "by": by, "ok": st["ok"], "fail": st["fail"]})

        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(work, ids))
            _set({"running": False, "done": done, "total": len(ids), "by": by, "finished": access.now(), "ok": st["ok"], "fail": st["fail"]})
        except Exception as e:  # noqa: BLE001
            _set({"running": False, "error": str(e)[:300], "by": by, "finished": access.now()})


def after_import(mids: list[int]):
    """导入后：内容变了的清掉旧结果；配了大模型就在后台补抽"""
    if mids:
        with db.tx() as c:
            c.execute(f"UPDATE meeting SET extracted_at = NULL WHERE id IN ({','.join('?' for _ in mids)})", mids)
    if llm.configured() and not _lock.locked():
        try:
            start("导入后自动")
        except RuntimeError:
            pass


def reset_running():
    st = state()
    if st.get("running"):
        _set({**{k: v for k, v in st.items() if k not in ("all", "extracted")}, "running": False, "error": "服务重启，抽取中断；点“补全抽取”继续"})
