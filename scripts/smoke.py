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
    check(f == {"均瑶云商", rules.BIZ}, "均瑶云商的业务会同时进研发市场销售")

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

    print("钉钉图文纪要 JSON 摘要")
    md = ingest.clean('{"meta_info":{"title":"标题A","subtitle":"副标题"},"overview":"总述","menu":[{"slice_id":"s1","title":"议题一","summary":"议题摘要"}],'
                      '"details":[{"slice_id":"s1","detail_json":{"blocks":[{"type":"callout","content":["要点1"]},{"type":"module","title":"模块","children":[{"type":"content-list","items":[{"title":"小标题","content":["内容"]}]}]}]}}]}')
    check("# 标题A" in md and "## 议题一" in md and "**小标题**：内容" in md and "{" not in md, "JSON 摘要转成可读文字")

    print(f"\n全部通过（{ok_count} 项）")


if __name__ == "__main__":
    main()
