"""分类规则（不靠大模型的那一层）。

目录（和管理员之前手工整理的文件夹一致）：
- 研发市场销售：研发、市场、销售合并的板块，也收各子公司的业务会
- 人事 / 财务 / IT / 董秘办：上海食品的职能部门
- 每日博士 / 大健康 / 均瑶云商 / 均瑶润盈 / 衢州食品 / 均瑶宜昌 / 泛缘：子公司，各自一个文件夹
"""
import re

BIZ = "研发市场销售"
FUNC = ["人事", "财务", "IT", "董秘办"]
SUBS = ["每日博士", "大健康", "均瑶云商", "均瑶润盈", "衢州食品", "均瑶宜昌", "泛缘"]
FOLDERS = [BIZ, *FUNC, *SUBS]

# 「所属组织」→ 主体。不在这里的组织不导入
ORGS = [
    ("上海食品", "上海食品"),
    ("每日博士", "每日博士"),
    ("云商", "均瑶云商"),
    ("大健康", "大健康"),
    ("润盈", "均瑶润盈"),
    ("衢州", "衢州食品"),
    ("宜昌", "均瑶宜昌"),
    ("泛缘", "泛缘"),
    ("测试部门", "测试部门"),
]

# 这些子公司的业务会默认也放进「研发市场销售」（管理员之前就是这么整理的），可在管理页改
DEFAULT_BIZ_SUBS = ["大健康", "均瑶云商"]


def entity_of(org: str) -> str | None:
    org = (org or "").strip()
    for key, name in ORGS:
        if key in org:
            return name
    return None


# 会议类型：命中越靠前越优先
TYPES = [
    ("面试", r"面试|候选人|求职|应聘|简历|岗位胜任|职业经历"),
    ("外部讲座", r"讲座|演讲|分享会|论坛|峰会|发布会|公开课|报告.*解读|家长会"),
    ("通话", r"语音通话|电话|通话确认|语音备忘"),
    ("经营分析", r"经营分析|复盘|月度|季度|半年|年度总结|业绩"),
    ("例会", r"例会|周会|晨会|早会|周例会"),
    ("培训", r"培训|学习会|操作指引"),
    ("客户沟通", r"客户|KA|渠道|经销商|拜访|合作|洽谈|对接"),
]

PATTERNS = {
    "人事": r"性骚扰|投诉|违纪|处分|员工手册|行为准则|企业文化|组织绩效|绩效体系|团建|培训|述职|管培|轮岗|升学|保送|新人|面试|招聘|候选人|入职|离职|薪酬|薪资|绩效考核|人事|HR|人力|编制|岗位|员工关系|劳动合同|社保|组织架构调整",
    "财务": r"财务|预算|核销|结算|报销|税务|税|审计|资金|现金流|应收|应付|计提|成本核算|对账|发票|毛利|费用管控|资产|挂账|负债",
    "IT": r"信息化|数字化转型|IT部|软件开发|服务器|ERP|\bOA\b|SAP|系统切换|系统上线|系统开发|系统对接|系统功能|数据中台|智能体|商旅平台|网络安全|账号权限",
    "董秘办": r"董事会|股东大会|证券|信披|信息披露|投资者关系|年报|交易所|问询函|董秘|上市公司|股权激励|并购|重组",
}
_PAT = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
_TYPE = [(n, re.compile(p, re.I)) for n, p in TYPES]
SENSITIVE_TYPES = {"面试"}
# 标题命中这些也算敏感：人事调查类、私人内容（子女教育讲座等）
SENSITIVE_TITLE = re.compile(r"性骚扰|违纪|处分|调查|举报|投诉处理|离职面谈|薪酬|工资|家庭教育|育儿|亲子|家长会|中考|高考|小升初|升学|保送|私人")


def meeting_type(title: str, text: str) -> str:
    # 只看标题：摘要里的词太杂，容易误判
    for name, p in _TYPE:
        if p.search(title):
            return name
    return "其他"


def score(title: str, keywords: str, summary: str) -> dict[str, float]:
    """每个职能部门的命中分：标题命中权重高，摘要命中按次数"""
    s = {}
    body = (keywords or "") + "\n" + (summary or "")[:4000]
    for k, p in _PAT.items():
        t = len(p.findall(title or ""))
        b = len(p.findall(body))
        s[k] = t * 3 + min(b, 12) * 0.5
    return s


def classify(org: str, title: str, keywords: str, summary: str, biz_subs=None) -> dict:
    """返回 {entity, folders, type, sensitive, confidence, reason}；entity 为 None 表示不导入"""
    entity = entity_of(org)
    if not entity:
        return {"entity": None}
    biz_subs = DEFAULT_BIZ_SUBS if biz_subs is None else biz_subs
    mtype = meeting_type(title or "", (keywords or "") + (summary or ""))
    sensitive = mtype in SENSITIVE_TYPES or bool(SENSITIVE_TITLE.search(title or ""))
    sc = score(title, keywords, summary)
    folders: list[str] = []
    conf = 0.9
    reason = ""
    if entity in SUBS:
        folders.append(entity)
        if entity in biz_subs and not sensitive and sc["人事"] < 3 and sc["财务"] < 4:
            folders.append(BIZ)
        reason = "按所属组织"
    elif entity == "测试部门":
        folders.append("IT")
        reason = "测试部门归 IT"
    else:  # 上海食品：按内容分到职能部门 / 研发市场销售
        if sensitive:
            folders.append("人事")
            reason = "敏感（面试 / 人事 / 私人内容）"
        else:
            hits = sorted(((v, k) for k, v in sc.items() if v >= 3.5), reverse=True)
            for v, k in hits[:2]:
                folders.append(k)
            # 业务会默认进研发市场销售；只有“纯职能”的会（人事、内部财务审计这类）不进
            pure_func = hits and (
                (hits[0][1] == "人事" and hits[0][0] >= 4)
                or (hits[0][1] in ("财务", "董秘办") and hits[0][0] >= 9 and sc["人事"] < 3 and mtype not in ("经营分析", "例会", "客户沟通"))
            )
            if not pure_func:
                folders.insert(0, BIZ)
            if not hits:
                conf = 0.6
            elif len(hits) > 1 and hits[0][0] - hits[1][0] < 1.5:
                conf = 0.55
            reason = "按内容关键词：" + ("、".join(f"{k}{v:g}" for v, k in hits) or "无明显职能特征")
    return {"entity": entity, "folders": folders, "type": mtype, "sensitive": sensitive, "confidence": conf, "reason": reason}
