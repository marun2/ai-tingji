"""大模型（兼容 OpenAI 接口）。地址、模型、密钥由管理员在「管理 → 模型设置」里填，存在本应用自己的数据库里。

本应用单独接大模型，不走企业中台的问一问（用户 2026-11-24 决定）。
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import db, rules

KEYS = ("llm_base_url", "llm_api_key", "llm_model", "llm_model_classify")


def settings() -> dict:
    rows = db.q("SELECT k, v FROM setting WHERE k IN (?, ?, ?, ?)", KEYS)
    s = {r["k"]: r["v"] or "" for r in rows}
    return {k: s.get(k, "") for k in KEYS}


def public_settings() -> dict:
    s = settings()
    key = s["llm_api_key"]
    return {"llm_base_url": s["llm_base_url"], "llm_model": s["llm_model"], "llm_model_classify": s["llm_model_classify"],
            "has_key": bool(key), "key_hint": (key[:3] + "…" + key[-4:]) if len(key) > 8 else ("已填写" if key else "")}


def save_settings(data: dict):
    with db.tx() as c:
        for k in KEYS:
            if k not in data:
                continue
            v = str(data[k] or "").strip()
            if k == "llm_api_key" and not v:
                continue  # 留空 = 不修改密钥
            if k == "llm_base_url":
                v = v.rstrip("/")
            if db.IS_PG:
                c.execute("INSERT INTO setting (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, v))
            else:
                c.execute("INSERT OR REPLACE INTO setting (k, v) VALUES (?, ?)", (k, v))


def configured() -> bool:
    s = settings()
    return bool(s["llm_base_url"] and s["llm_api_key"] and s["llm_model"])


class LLMError(Exception):
    pass


def chat(messages: list[dict], model: str | None = None, temperature: float = 0.2, max_tokens: int = 1200, timeout: int = 120, task: str = "other") -> str:
    s = settings()
    if not (s["llm_base_url"] and s["llm_api_key"] and s["llm_model"]):
        raise LLMError("还没有配置大模型（管理 → 模型设置）")
    url = s["llm_base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        if not re.search(r"/v\d+[a-z]*$", url):
            url += "/v1"   # 只填了域名（如 https://api.jiceapi.com）时补上 /v1
        url += "/chat/completions"
    payload = json.dumps({"model": model or s["llm_model"], "messages": messages, "temperature": temperature,
                          "max_tokens": max_tokens, "stream": False}).encode()
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {s['llm_api_key']}", "Content-Type": "application/json"})
        wait = 3 * (attempt + 1)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            _record(task, data.get("usage") or {}, messages, data)
            ch = (data.get("choices") or [{}])[0]
            content = (ch.get("message") or {}).get("content") or ""
            if not content.strip():
                last = "大模型返回了空内容" + ("（输出长度不够，被截断）" if ch.get("finish_reason") == "length" else "")
            else:
                if ch.get("finish_reason") == "length":
                    content += "\n[[TRUNCATED]]"
                return content
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last = f"大模型接口返回 {e.code}：{body}"
            if e.code in (400, 401, 403, 404):
                break  # 配置问题，重试也没用
            if e.code == 429 or e.code >= 500:          # 太频繁 / 中转站忙：等久一点
                try:
                    wait = max(wait, min(60, int(e.headers.get("Retry-After") or 0)))
                except (TypeError, ValueError):
                    pass
                wait = max(wait, 10 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = f"连接大模型失败：{e}"
        time.sleep(wait)
    raise LLMError(last or "大模型调用失败")


def _record(task: str, usage: dict, messages: list[dict], data: dict):
    """记用量：接口返回了 usage 就用它，没有就按字数粗估（中文约 1 字 ≈ 1 token）"""
    try:
        pt = int(usage.get("prompt_tokens") or 0) or sum(len(str(m.get("content", ""))) for m in messages)
        ct = int(usage.get("completion_tokens") or 0) or len(str(data["choices"][0]["message"].get("content") or ""))
        day = time.strftime("%Y-%m-%d")
        with db.tx() as c:
            if db.IS_PG:
                c.execute("INSERT INTO llm_usage (day, task, calls, prompt_tokens, completion_tokens) VALUES (?, ?, 1, ?, ?) "
                          "ON CONFLICT (day, task) DO UPDATE SET calls = llm_usage.calls + 1, prompt_tokens = llm_usage.prompt_tokens + EXCLUDED.prompt_tokens, "
                          "completion_tokens = llm_usage.completion_tokens + EXCLUDED.completion_tokens", (day, task, pt, ct))
            else:
                c.execute("INSERT INTO llm_usage (day, task, calls, prompt_tokens, completion_tokens) VALUES (?, ?, 1, ?, ?) "
                          "ON CONFLICT (day, task) DO UPDATE SET calls = calls + 1, prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
                          "completion_tokens = completion_tokens + excluded.completion_tokens", (day, task, pt, ct))
    except Exception as e:  # noqa: BLE001
        print("记录大模型用量失败：", e, flush=True)


TASK_NAMES = {"classify": "分类", "extract": "信息抽取", "ask": "提问", "report": "周报月报", "test": "测试", "other": "其他"}


def usage(month: str) -> dict:
    rows = db.q("SELECT task, SUM(calls) AS calls, SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct FROM llm_usage WHERE day LIKE ? GROUP BY task", (month + "%",))
    days = db.q("SELECT day, SUM(prompt_tokens + completion_tokens) AS t FROM llm_usage WHERE day LIKE ? GROUP BY day ORDER BY day", (month + "%",))
    total_in = sum(int(r["pt"] or 0) for r in rows)
    total_out = sum(int(r["ct"] or 0) for r in rows)
    b = {r["k"]: r["v"] for r in db.q("SELECT k, v FROM setting WHERE k IN ('budget_tokens', 'price_in', 'price_out')")}
    def num(k):
        try:
            return float(b.get(k) or 0)
        except ValueError:
            return 0.0
    budget, pin, pout = num("budget_tokens"), num("price_in"), num("price_out")
    cost = (total_in * pin + total_out * pout) / 1_000_000 if (pin or pout) else None
    return {"month": month, "by_task": [{"task": TASK_NAMES.get(r["task"], r["task"]), "calls": int(r["calls"] or 0), "in": int(r["pt"] or 0), "out": int(r["ct"] or 0)} for r in rows],
            "days": [{"day": d["day"], "tokens": int(d["t"] or 0)} for d in days], "total_in": total_in, "total_out": total_out,
            "budget_tokens": budget, "price_in": pin, "price_out": pout, "cost": cost,
            "over": bool(budget and total_in + total_out > budget), "near": bool(budget and total_in + total_out > budget * 0.8)}


def save_budget(d: dict):
    with db.tx() as c:
        for k in ("budget_tokens", "price_in", "price_out"):
            if k in d:
                try:
                    v = str(max(0.0, float(d[k] or 0)))
                except (TypeError, ValueError):
                    v = "0"
                if db.IS_PG:
                    c.execute("INSERT INTO setting (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, v))
                else:
                    c.execute("INSERT OR REPLACE INTO setting (k, v) VALUES (?, ?)", (k, v))


def parse_json(text: str) -> dict:
    """从回答里取出 JSON：去掉 ```json 包裹、思考过程；被截断的尽量补全括号"""
    t = text or ""
    truncated = "[[TRUNCATED]]" in t
    t = t.replace("[[TRUNCATED]]", "")
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    t = re.sub(r"```(?:json)?", "", t)
    start = t.find("{")
    if start < 0:
        raise LLMError("大模型没有按格式回答（没有 JSON）")
    body = t[start:]
    end = body.rfind("}")
    for cand in ([body[:end + 1]] if end >= 0 else []) + [body]:
        try:
            return json.loads(cand)
        except ValueError:
            pass
        # 修补：去掉末尾逗号、补齐没闭合的括号和引号
        c = re.sub(r",\s*([}\]])", r"\1", cand.rstrip().rstrip(","))
        if c.count('"') % 2:
            c += '"'
        stack = []
        for ch in re.sub(r'"(?:\\.|[^"\\])*"', "", c):
            if ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        try:
            return json.loads(re.sub(r",\s*$", "", c) + "".join(reversed(stack)))
        except ValueError:
            continue
    raise LLMError("大模型的回答" + ("被截断了，" if truncated else "") + "格式不对，没法解析")


CLASSIFY_PROMPT = """你是均瑶健康的会议归档助手。根据会议信息，判断这场会应该放进哪些文件夹（可以多个）。

文件夹（只能从这里选）：
- 研发市场销售：产品研发、配方、品牌、市场推广、渠道、销售、客户、经营分析、业务例会等业务类会议（大多数会议属于这里）
- 人事：面试、招聘、培训、述职、员工管理、组织架构调整、绩效薪酬
- 财务：预算、费用核销、结算、审计、资金、税务、报表、内控
- IT：信息化系统、软件、数字化工具、AI 工具落地
- 董秘办：董事会、股东、证券、信息披露、投资者、交易所、并购重组、集团层面的重要汇报

规则：
- 一场经营分析会如果重点讨论了费用和利润，可以同时放「研发市场销售」和「财务」
- 面试、个人隐私类（私人通话、子女升学讲座等）不要放「研发市场销售」，sensitive 设为 true
- 拿不准时 confidence 给低一点

只输出 JSON：{"folders": ["…"], "type": "例会|经营分析|面试|客户沟通|外部讲座|通话|培训|其他", "sensitive": false, "confidence": 0.0到1.0, "reason": "一句话理由"}"""

ALLOWED = {rules.BIZ, *rules.FUNC}


def classify(title: str, creator: str, keywords: str, summary: str, rule_guess: list[str]) -> dict:
    s = settings()
    content = (f"标题：{title}\n创建人：{creator}\n关键词：{keywords or '无'}\n"
               f"规则初判：{'、'.join(rule_guess) or '无'}\n\n摘要：\n{(summary or '')[:3500]}")
    text = chat([{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": content}],
                model=s["llm_model_classify"] or None, max_tokens=800, timeout=90, task="classify")
    d = parse_json(text)
    folders = [f for f in d.get("folders") or [] if f in ALLOWED]
    try:
        conf = float(d.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    return {"folders": folders, "type": str(d.get("type") or "其他")[:10], "sensitive": bool(d.get("sensitive")),
            "confidence": max(0.0, min(1.0, conf)), "reason": str(d.get("reason") or "")[:200]}


def test() -> dict:
    t = time.time()
    out = chat([{"role": "user", "content": "只回复两个字：收到"}], max_tokens=10, timeout=30, task="test")
    return {"ok": True, "reply": out.strip()[:50], "seconds": round(time.time() - t, 1)}
