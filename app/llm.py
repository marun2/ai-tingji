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


def chat(messages: list[dict], model: str | None = None, temperature: float = 0.2, max_tokens: int = 1200, timeout: int = 120) -> str:
    s = settings()
    if not (s["llm_base_url"] and s["llm_api_key"] and s["llm_model"]):
        raise LLMError("还没有配置大模型（管理 → 模型设置）")
    url = s["llm_base_url"]
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    payload = json.dumps({"model": model or s["llm_model"], "messages": messages, "temperature": temperature,
                          "max_tokens": max_tokens, "stream": False}).encode()
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {s['llm_api_key']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            last = f"大模型接口返回 {e.code}：{body}"
            if e.code in (400, 401, 403, 404):
                break  # 配置问题，重试也没用
        except Exception as e:  # noqa: BLE001
            last = f"连接大模型失败：{e}"
        time.sleep(2 * (attempt + 1))
    raise LLMError(last or "大模型调用失败")


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise LLMError("大模型没有按格式回答")
    return json.loads(m.group(0))


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
                model=s["llm_model_classify"] or None, max_tokens=300, timeout=90)
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
    out = chat([{"role": "user", "content": "只回复两个字：收到"}], max_tokens=10, timeout=30)
    return {"ok": True, "reply": out.strip()[:50], "seconds": round(time.time() - t, 1)}
