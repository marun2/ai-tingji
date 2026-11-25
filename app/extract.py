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

ENTITY_TYPES = ["客户", "平台", "产品", "竞品", "项目", "区域"]
_lock = threading.Lock()

PROMPT = """你在整理均瑶健康（食品饮料公司）的会议记录。从下面这场会的摘要里提取信息，只输出 JSON：
{"decisions": ["会上做出的决定/定下的事，每条一句话，最多 6 条"],
 "risks": ["暴露出的问题、风险、困难，每条一句话，最多 5 条"],
 "metrics": [{"name": "指标名，如 销售额/回款/利润/产能/铺市率/库存/费用/目标", "value": "数值，照原文", "unit": "单位", "context": "一句话说明是谁/哪里/什么时间的"}],
 "entities": {"客户": ["零售客户/渠道名，如 盒马、叮咚、7-Eleven、美宜佳（不含电商和内容平台）"], "平台": ["电商或内容平台，如 京东、天猫、抖音、拼多多、小红书、视频号"], "产品": ["公司自己的产品/品类，如 营养瓶、椰子水、益生菌"],
              "竞品": ["竞争对手品牌，如 李子园"], "项目": ["项目或专项名，如 CNY、锤石项目"], "区域": ["地区/大区，如 浙江、东北大区、西南"]},
 "topics": ["3～5 个概括这场会主题的短标签，如 春节备货、渠道费用、新品上市"]}
规则：
- 只写摘要里明确出现的，不要推测；没有就给空数组
- 不要写人名（客户里也不要写个人名字，只写公司/渠道/品牌）
- 名称用最常见的简称，同一个东西用同一个写法（如 “盒马鲜生”写成“盒马”）
- metrics 最多 8 条，只要有具体数字的"""


def _norm(name: str) -> str:
    from . import synonyms
    n = re.sub(r"[\s“”\"'《》【】()（）]", "", str(name or ""))
    n = synonyms.canonical(n)
    n = re.sub(r"(有限公司|公司|集团|超市|鲜生|品牌)$", "", n)
    return synonyms.canonical(n)[:30]


def extract_one(m: dict) -> dict:
    content = f"会议：{m['title']}（{(m.get('start_time') or '')[:10]}）\n\n摘要：\n{(m.get('summary') or '')[:4500]}"
    if m.get("todos"):
        content += f"\n\n待办：\n{m['todos'][:800]}"
    if len(m.get("summary") or "") < 200 and m.get("transcript"):
        content += f"\n\n原文节选：\n{m['transcript'][:3000]}"
    s = llm.settings()
    msgs = [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}]
    text = llm.chat(msgs, model=s["llm_model_classify"] or None, max_tokens=3000, timeout=180, task="extract")
    try:
        return llm.parse_json(text)
    except llm.LLMError:
        # 偶尔回答格式不对（多半是文字里夹了英文双引号、或者写了一半）：提醒一次再要一遍，内容精简些
        msgs += [{"role": "assistant", "content": (text or "")[:1500]},
                 {"role": "user", "content": "上面的回答不是合法 JSON。请重新只输出一个合法的 JSON 对象，不要任何解释；"
                                             "字符串里不要用英文双引号（需要引号用中文“”）；每类最多 5 条，句子写短一些。"}]
        text = llm.chat(msgs, model=s["llm_model_classify"] or None, max_tokens=3000, timeout=180, task="extract")
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
        reasons = collections.Counter()
        examples: dict[str, str] = {}
        done = 0
        _set({"running": True, "done": 0, "total": len(ids), "by": by, "started": access.now()})
        lk = threading.Lock()

        stop = {"why": ""}

        def work(mid):
            nonlocal done
            if stop["why"]:                      # 余额不足等：后面的不再一个个去撞，直接跳过（下次补全会重试）
                st["fail"] += 1
                reasons[stop["why"]] += 1
                with lk:
                    done += 1
                return
            m = db.q1("SELECT id, title, start_time, summary, todos, transcript FROM meeting WHERE id = ?", (mid,))
            try:
                if m and ((m["summary"] or "").strip() or (m["transcript"] or "").strip()):
                    _save(mid, extract_one(m))
                    st["ok"] += 1
                else:
                    st["skip"] += 1
            except Exception as e:  # noqa: BLE001
                st["fail"] += 1
                reasons[_reason(e)] += 1
                examples.setdefault(_reason(e), str(e)[:200])
                if _reason(e) in ("中转站余额或额度不足", "密钥无效或没有权限"):
                    stop["why"] = _reason(e)
                print(f"抽取失败 #{mid}：{e}", flush=True)
            with lk:
                done += 1
                if done % 10 == 0 or done == len(ids):
                    _set({"running": True, "done": done, "total": len(ids), "by": by, "ok": st["ok"], "fail": st["fail"],
                          "errors": [{"reason": r, "count": n, "example": examples.get(r, "")} for r, n in reasons.most_common(5)]})

        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(work, ids))
            _set({"running": False, "done": done, "total": len(ids), "by": by, "finished": access.now(), "ok": st["ok"], "fail": st["fail"],
                  "errors": [{"reason": r, "count": n, "example": examples.get(r, "")} for r, n in reasons.most_common(5)]})
        except Exception as e:  # noqa: BLE001
            _set({"running": False, "error": str(e)[:300], "by": by, "finished": access.now()})


def _reason(e: Exception) -> str:
    """把出错信息归成几类，管理页上好看懂"""
    m = str(e)
    if re.search(r"余额|额度|quota|insufficient|balance|402|欠费", m, re.I):
        return "中转站余额或额度不足"
    if "429" in m:
        return "请求太频繁，被中转站限流"
    if re.search(r"返回 5\d\d", m):
        return "中转站或模型服务出错（5xx）"
    if re.search(r"返回 40[13]", m):
        return "密钥无效或没有权限"
    if "timed out" in m or "超时" in m or "timeout" in m.lower():
        return "等待回答超时"
    if "连接大模型失败" in m:
        return "连不上中转站"
    if "截断" in m:
        return "回答太长被截断"
    if "格式" in m or "JSON" in m or isinstance(e, ValueError):
        return "回答格式不对"
    if "空内容" in m:
        return "返回了空内容"
    return "其他：" + m[:40]


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
    """服务重启时：上次没抽完的，自动接着抽（只抽还没抽过的）"""
    st = state()
    if st.get("running"):
        _set({**{k: v for k, v in st.items() if k not in ("all", "extracted")}, "running": False})
        if llm.configured():
            try:
                start("服务重启后继续")
            except RuntimeError:
                pass
