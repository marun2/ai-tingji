"""冒烟测试（CI 也跑）：造一份假的听记 Excel，走一遍导入 → 去重 → 合并 → 分类 → 权限。

用法：python scripts/smoke.py
不需要真实数据，也不需要大模型；用临时目录里的 SQLite。
"""
import os
import pathlib
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="tingji-smoke-")
os.environ["DATA_DIR"] = TMP
os.environ.pop("DATABASE_URL", None)
os.environ.pop("ZT_APP", None)
os.environ["EMBED"] = "off"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402

from app import access, db, ingest, rules  # noqa: E402

ok_count = 0


def check(cond, msg):
    global ok_count
    if not cond:
        print("  ✗", msg)
        sys.exit(1)
    ok_count += 1
    print("  ✓", msg)


def make_excel(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "听记汇总"
    ws.append(list(ingest.COLS))
    rows = [
        # 标题, 创建人, 开始时间, 时长, 关键词, 摘要, 待办, 原文, 链接, 组织
        ("11-18 豫鲁三区生意回顾与CNY规划", "张三", "2026-11-18 08:46", "104", "渠道, 销售", "## 会议背景\n区域销售复盘，经销商动销与 CNY 货盘。![图](https://x.oss-cn-hangzhou.aliyuncs.com/a.png){origin:1,2}", "张三：发货表", "[发言人 1] 大家好" * 50, "A1", "均瑶集团上海食品有限公司"),
        ("会议录制：11-18 豫鲁三区生意回顾与CNY规划", "张三", "2026-11-18 08:47", "103", "", "短摘要", "", "", "A2", "均瑶集团上海食品有限公司"),
        ("【面试】 11月20日 14:00-线下销售负责人-视频面试-某某", "李四", "2026-11-20 13:57", "37", "", "## 面试概况\n候选人经历介绍" * 5, "", "x" * 300, "B1", "均瑶集团上海食品有限公司"),
        ("11-19 财务KPI指标与降本考核方案", "王五", "2026-11-19 11:03", "23", "预算, 费用", "预算、费用核销、审计、资金、现金流、应收、计提、税务" * 3, "", "y" * 300, "C1", "均瑶集团上海食品有限公司"),
        ("11-20 醒力罐品牌定调与物料分工", "赵六", "2026-11-20 11:02", "58", "", "品牌会议 产品 包装" * 10, "", "z" * 300, "D1", "每日博士（上海）商贸有限公司"),
        ("11-18 益生菌品类战略", "钱七", "2026-11-18 17:29", "23", "", "益生菌 品类 战略 渠道" * 10, "", "w" * 300, "E1", "上海均瑶云商网络科技有限公司"),
        ("名单外公司的会", "孙八", "2026-11-18 10:00", "10", "", "内容" * 50, "", "", "F1", "上海璟瑞企业管理有限公司"),
        ("8月14日以前的会", "孙八", "2026-08-01 10:00", "10", "", "内容" * 50, "", "", "G1", "均瑶集团上海食品有限公司"),
        ("空记录", "孙八", "2026-11-18 11:00", "1", "", "", "", "", "H1", "均瑶集团上海食品有限公司"),
        (None, None, None, None, None, None, None, "残留的原文行", "I1", "均瑶集团上海食品有限公司"),
        ("截断的长会", "周九", "2026-11-17 09:00", "200", "", "经营分析 销售" * 20, "", "长" * ingest.EXCEL_CELL_MAX, "J1", "均瑶集团上海食品有限公司"),
    ]
    for r in rows:
        ws.append(list(r[:8]) + ["view", r[9]])
        cell = ws.cell(ws.max_row, 9)
        cell.hyperlink = f"https://shanji.dingtalk.com/app/transcribes/{r[8]}"
    wb.save(path)


def main():
    print("临时数据目录：", TMP)
    db.init()
    xlsx = pathlib.Path(TMP) / "test.xlsx"
    make_excel(xlsx)

    print("导入")
    with db.tx() as c:
        bid = db.insert(c, "import_batch", {"filename": "test.xlsx", "uploaded_by": "smoke", "status": "running", "created_at": access.now()})
    ingest._run_import(bid, str(xlsx))
    b = db.q1("SELECT * FROM import_batch WHERE id = ?", (bid,))
    check(b["status"] == "done", f"导入完成（{b['error'] or ''}）")
    import json
    st = json.loads(b["stats"])
    check(st.get("no_title") == 1, "没有标题的残留行被丢掉")
    check(st.get("other_org") == 1, "名单外主体不导入")
    check(st.get("before_start") == 1, "8月14日以前的不导入")
    check(st.get("merged") == 1, "「会议录制：」和同名听记合并成一场")
    check(st.get("empty") == 1, "空记录单独标记")
    check(st.get("truncated") == 1, "被 Excel 截断的原文有标记")

    m = db.q1("SELECT * FROM meeting WHERE title LIKE ?", ("%豫鲁三区%",))
    check(not m["title"].startswith("会议录制"), "合并后用不带「会议录制」的标题")
    check("aliyuncs" not in m["summary"] and "origin:" not in m["summary"], "摘要里的图片外链和录音位置标记被去掉")
    check(len(db.q("SELECT * FROM meeting_link WHERE meeting_id = ?", (m["id"],))) == 2, "两个听记链接都记下来（以后去重用）")

    def folders(title):
        r = db.q1("SELECT id, sensitive, status FROM meeting WHERE title LIKE ?", (f"%{title}%",))
        return r, {x["folder"] for x in db.q("SELECT folder FROM meeting_folder WHERE meeting_id = ?", (r["id"],))}

    r, f = folders("面试")
    check(r["sensitive"] == 1 and rules.BIZ not in f and "人事" in f, "面试 → 人事、敏感，不进研发市场销售")
    r, f = folders("财务KPI")
    check("财务" in f, "财务类会议进财务")
    r, f = folders("醒力罐")
    check(f == {"每日博士"}, "每日博士的会只在每日博士")
    r, f = folders("益生菌品类")
    check(f == {"均瑶云商"}, "子公司的会不进研发市场销售（用户决定）")

    print("再导一次同一个文件")
    with db.tx() as c:
        bid2 = db.insert(c, "import_batch", {"filename": "test.xlsx", "uploaded_by": "smoke", "status": "running", "created_at": access.now()})
    ingest._run_import(bid2, str(xlsx))
    st2 = json.loads(db.q1("SELECT stats FROM import_batch WHERE id = ?", (bid2,))["stats"])
    check(not st2.get("new") and st2.get("dup_existing") == 8, f"重复导入全部跳过（{st2}）")

    print("权限")
    lead = {"id": "x", "name": "负责人", "dept": "sales", "role": "biz", "is_admin": False, "see_all": False}
    board = {"id": "y", "name": "董事", "dept": "board", "role": "all", "is_admin": False, "see_all": True}
    cond, params = access.meeting_filter(lead)
    seen = {x["title"] for x in db.q(f"SELECT m.title FROM meeting m WHERE {cond}", params)}
    check(not any("面试" in t for t in seen), "部门负责人看不到面试")
    check(any("豫鲁三区" in t for t in seen), "部门负责人能看到研发市场销售里的业务会")
    check(not any("醒力罐" in t for t in seen), "部门负责人看不到每日博士")
    check(access.folders_for(lead) == [rules.BIZ], "部门负责人只有研发市场销售一个文件夹")
    cond, params = access.meeting_filter(board)
    check(len(db.q(f"SELECT m.id FROM meeting m WHERE {cond}", params)) >= 6, "董事局能看到全部（空记录除外）")

    print("历史听记包")
    import gzip
    pack = pathlib.Path(TMP) / "h.json.gz"
    with gzip.open(pack, "wt", encoding="utf-8") as fh:
        json.dump({"kind": "tingji-history", "items": [
            {"id": "hist:1", "title": "03-30 内部会议_ 定制业务与产能规划", "start_time": "2026-03-30 10:00", "entity": "上海食品",
             "folders": [rules.BIZ, "财务"], "summary": "产能规划 定制业务" * 10, "transcript": "原文" * 200},
            {"id": "hist:2", "title": "【面试】 03月17日 10_30-拼多多运营", "start_time": "2026-03-17 10:30", "entity": "上海食品",
             "folders": [rules.BIZ], "summary": "面试概况" * 10, "transcript": "x" * 300},
            {"id": "hist:3", "title": "日期不详的会", "start_time": None, "entity": "每日博士", "folders": ["每日博士"], "summary": "内容" * 30, "transcript": ""},
            {"id": "hist:4", "title": "8月以后的", "start_time": "2026-08-20 10:00", "entity": "上海食品", "folders": [rules.BIZ], "summary": "内容" * 30, "transcript": ""},
        ]}, fh, ensure_ascii=False)
    with db.tx() as c:
        bid3 = db.insert(c, "import_batch", {"filename": "h.json.gz", "uploaded_by": "smoke", "status": "running", "created_at": access.now()})
    ingest._run_import(bid3, str(pack))
    b3 = db.q1("SELECT * FROM import_batch WHERE id = ?", (bid3,))
    st3 = json.loads(b3["stats"] or "{}")
    check(b3["status"] == "done" and st3.get("new") == 3 and st3.get("after_start") == 1 and st3.get("no_date") == 1, f"历史包导入：3 场新增、8月14日以后跳过（{st3}）")
    r, f = folders("定制业务与产能规划")
    check(f == {rules.BIZ, "财务"}, "历史文件夹归属原样保留")
    r, f = folders("拼多多运营")
    check(r["sensitive"] == 1 and rules.BIZ not in f, "历史里的面试也不会进研发市场销售")

    print("检索：文本段 + 向量（用假模型代替真模型，只测流程）")
    import numpy as np
    from app import chunks, embed, exporter, qa
    n_chunks = db.q1("SELECT COUNT(*) AS n FROM chunk")["n"]
    check(n_chunks > 0, f"导入后自动切成检索文本段（{n_chunks} 段）")

    class FakeTok:
        def encode(self, t, max_len=512):
            return [101] + [ord(c) % 5000 for c in t[: max_len - 2]] + [102]

    class FakeSession:
        class _I:
            def __init__(self, n): self.name = n
        def get_inputs(self): return [self._I("input_ids"), self._I("attention_mask"), self._I("token_type_ids")]
        def run(self, _o, feeds):
            ids = feeds["input_ids"]
            out = np.zeros((ids.shape[0], ids.shape[1], embed.DIM), dtype=np.float32)
            for i, row in enumerate(ids):
                for t in row:
                    if t:
                        out[i, 0, int(t) % embed.DIM] += 1.0   # 字袋：用字相近的文本向量也相近
            return [out]

    embed._session, embed._tok = FakeSession(), FakeTok()
    rows = db.q("SELECT id, meeting_id, text FROM chunk ORDER BY id")
    v = embed.embed([r["text"] for r in rows]).astype(np.float16)
    embed._ids = np.array([r["id"] for r in rows], dtype=np.int64)
    embed._mids = np.array([r["meeting_id"] for r in rows], dtype=np.int64)
    embed._vecs = v
    m_biz = db.q1("SELECT id FROM meeting WHERE title LIKE ?", ("%豫鲁三区%",))["id"]
    hits = embed.search(embed.embed(["豫鲁三区生意回顾 CNY 货盘"], query=True)[0], {m_biz}, top=5)
    check(hits and all(db.q1("SELECT meeting_id FROM chunk WHERE id = ?", (c,))["meeting_id"] == m_biz for c, _ in hits), "向量检索只在允许的会议里找")
    q = {"mode": "detail", "keywords": ["面试", "候选人"], "date_from": "", "date_to": "", "rewrite": "面试情况"}
    got = qa.retrieve(lead, "面试情况", "", q)
    check(all("面试" not in r["title"] for r in got), "部门负责人提问时检索不到面试内容")
    got = qa.retrieve(board, "面试情况", "", q)
    check(any("面试" in r["title"] for r in got), "董事局提问时能检索到面试内容")
    embed.forget_meetings([m_biz])
    check(m_biz not in set(embed._mids.tolist()), "会议内容更新后旧向量被清掉（后台会重新生成）")

    print("导出 Word")
    name, data, n = exporter.export(board, ids=[m_biz])
    check(name.endswith(".docx") and n == 1 and data[:2] == b"PK", "单场会导出 Word")
    name, data, n = exporter.export(board, folder=rules.BIZ)
    check(name.endswith(".zip") and n >= 2, f"整个文件夹导出 zip（{n} 场）")
    try:
        exporter.export(lead, folder="人事")
        check(False, "部门负责人不能导出人事")
    except PermissionError:
        check(True, "部门负责人不能导出人事")
    name, data, n = exporter.export(lead, folder=rules.BIZ)
    import zipfile as _zf
    names = _zf.ZipFile(__import__("io").BytesIO(data)).namelist() if name.endswith(".zip") else [name]
    check(not any("面试" in x for x in names), "部门负责人导出的研发市场销售里没有面试")

    print("抽取 / 报告 / 可视化（不调大模型，直接写入抽取结果）")
    from app import extract, insights, reports, synonyms, terms
    synonyms.seed(); synonyms.migrate_v2(); synonyms.migrate_v3(); terms.seed()
    m_int = db.q1("SELECT id FROM meeting WHERE title LIKE ?", ("%拼多多运营%",))["id"]
    extract._save(m_biz, {"decisions": ["12月15日前完成85%回款"], "risks": ["长沙断货"], "metrics": [{"name": "回款", "value": "85", "unit": "%", "context": "12月15日前"}],
                          "entities": {"客户": ["盒马鲜生", "叮咚", "经销商"], "产品": ["每日博士", "微动力", "饮料"], "竞品": ["李子园"], "区域": ["豫鲁三区"]}, "topics": ["春节备货"]})
    extract._save(m_int, {"decisions": [], "risks": ["候选人期望薪资高"], "metrics": [], "entities": {"客户": ["拼多多"]}, "topics": ["招聘"]})
    check(db.q1("SELECT text FROM fact WHERE meeting_id = ? AND kind = 'entity' AND label = '客户' ORDER BY id LIMIT 1", (m_biz,))["text"] == "盒马",
          "抽取结果入库，名称统一（盒马鲜生 → 盒马）")
    st = extract.state()
    check(st["extracted"] >= 2, "抽取进度可查")
    g_lead = insights.graph(lead, days=730)
    names = {n["name"] for n in g_lead["nodes"]}
    check("盒马" in names and "招聘" not in names and "拼多多" not in names, "部门负责人的知识图谱里没有面试会里的内容")
    g_board = insights.graph(board, days=730)
    bn = {(n["type"], n["name"]) for n in g_board["nodes"]}
    check(("话题", "招聘") not in bn and ("话题", "春节备货") in bn, "面试会议不进洞察（董事局也一样，知识库里照样能看）")
    print("关键词修正")
    check(("客户", "经销商") not in bn, "太笼统的词（经销商）默认不显示")
    check(("产品", "每日博士") not in bn, "和子公司同名的词不当成产品")
    check(("产品", "味动力") in bn and ("产品", "微动力") not in bn, "“微动力”统一成“味动力”")
    terms.set_rule("饮料", "type", "话题")
    bn = {(n["type"], n["name"]) for n in insights.graph(board, days=730)["nodes"]}
    check(("话题", "饮料") in bn and ("产品", "饮料") not in bn, "管理员把“饮料”改成话题后，图谱跟着变")
    terms.set_rule("饮料", "hide")
    check("饮料" not in {n["name"] for n in insights.graph(board, days=730)["nodes"]}, "设为不显示后图谱里没有了")
    terms.set_rule("饮料", "clear")
    check(("产品", "饮料") in {(n["type"], n["name"]) for n in insights.graph(board, days=730)["nodes"]}, "撤销后恢复原样")
    terms.merge("叮咚", "叮咚买菜")
    check(db.q1("SELECT 1 FROM fact WHERE meeting_id = ? AND text = '叮咚买菜'", (m_biz,)) is not None and synonyms.canonical("叮咚") == "叮咚买菜", "在图谱里合并两个词，已抽取的名称跟着统一")
    terms.merge("叮咚买菜", "叮咚")
    check(isinstance(insights.collab(board, days=730)["folders"], list) and isinstance(insights.river(board)["series"], list), "部门协作、话题河流能出数据")
    ck = insights.cockpit(lead)
    check(all("拼多多" not in (r["title"] or "") for r in ck["risks"]), "部门负责人的驾驶舱看不到面试里的风险")
    data = reports.collect("biz", "2026-11-01", "2026-11-30")
    check(all("面试" not in m["title"] for m in data["meetings"]) and data["stats"]["meetings"] >= 1, "研发市场销售版报告不含面试")
    with db.tx() as c:
        rid = db.insert(c, "report", {"kind": "week", "scope": "all", "period_start": "2026-11-16", "period_end": "2026-11-22", "title": "t",
                                      "content": f"测试 [M{m_int}]", "stats": "{}", "created_by": "smoke", "created_at": access.now()})
    try:
        reports.get_report(lead, rid)
        check(False, "部门负责人看不到全公司版报告")
    except PermissionError:
        check(True, "部门负责人看不到全公司版报告")
    check(str(m_int) in {str(k) for k in reports.get_report(board, rid)["refs"]}, "报告里的会议出处能打开")
    s, e = reports.period("week", __import__("datetime").date(2026, 11, 24))
    check((s, e) == ("2026-11-16", "2026-11-22"), "周报周期：上周一到上周日")
    s, e = reports.period("month", __import__("datetime").date(2026, 12, 1))
    check((s, e) == ("2026-11-01", "2026-11-30"), "月报周期：上个月")

    print("同义词 / 用量")
    from app import synonyms, llm as _llm
    synonyms.seed()
    check("河马" in synonyms.expand("盒马") and synonyms.canonical("胃动力") == "味动力" and synonyms.canonical("君耀健康") == "均瑶健康", "同义词表：盒马/河马、味动力/胃动力、均瑶健康/君耀健康 当成一回事")
    synonyms.migrate_v2()
    check(not db.q1("SELECT 1 FROM synonym WHERE canonical = '君耀健康'"), "君耀健康 并入 均瑶健康")
    import app.main as _m
    with db.tx() as c:
        c.execute("UPDATE meeting SET summary = summary || ' 河马渠道' WHERE id = ?", (m_biz,))
    r = _m._search(board, "盒马", "")
    check(any(x["id"] == m_biz for x in r["items"]), "搜“盒马”能找到写成“河马”的会")
    _llm._record("ask", {"prompt_tokens": 1000, "completion_tokens": 200}, [], {"choices": [{"message": {"content": ""}}]})
    _llm.save_budget({"budget_tokens": 1000})
    us = _llm.usage(__import__("time").strftime("%Y-%m"))
    check(us["total_in"] >= 1000 and us["over"], "大模型用量按月统计，超出额度会提醒")

    print("钉钉图文纪要 JSON 摘要")
    md = ingest.clean('{"meta_info":{"title":"标题A","subtitle":"副标题"},"overview":"总述","menu":[{"slice_id":"s1","title":"议题一","summary":"议题摘要"}],'
                      '"details":[{"slice_id":"s1","detail_json":{"blocks":[{"type":"callout","content":["要点1"]},{"type":"module","title":"模块","children":[{"type":"content-list","items":[{"title":"小标题","content":["内容"]}]}]}]}}]}')
    check("# 标题A" in md and "## 议题一" in md and "**小标题**：内容" in md and "{" not in md, "JSON 摘要转成可读文字")

    print(f"\n全部通过（{ok_count} 项）")


if __name__ == "__main__":
    main()
