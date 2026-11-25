-- PostgreSQL（服务器）。和 schema.sql 保持一致
CREATE TABLE IF NOT EXISTS meeting (
  id SERIAL PRIMARY KEY,
  link TEXT,                       -- 听记链接（主记录）
  title TEXT NOT NULL,
  creator TEXT,
  start_time TEXT,                 -- 'YYYY-MM-DD HH:MM'
  duration INTEGER,                -- 分钟
  keywords TEXT,
  summary TEXT,
  todos TEXT,
  transcript TEXT,
  truncated INTEGER DEFAULT 0,     -- 原文被 Excel 截断
  org TEXT,                        -- 所属组织（原文）
  entity TEXT,                     -- 主体
  mtype TEXT,                      -- 会议类型
  sensitive INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ok',        -- ok 正常 / pending 待确认 / empty 空记录
  confidence DOUBLE PRECISION,
  reason TEXT,
  classified_by TEXT,              -- rule / llm / manual
  batch_id INTEGER,
  created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  updated_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS ix_meeting_time ON meeting(start_time);
CREATE INDEX IF NOT EXISTS ix_meeting_status ON meeting(status);

-- 一场会可能有多个听记链接（「会议录制：xxx」和听记是同一场会）；链接用来去重
CREATE TABLE IF NOT EXISTS meeting_link (
  link_id TEXT PRIMARY KEY,
  meeting_id INTEGER NOT NULL,
  title TEXT
);
CREATE INDEX IF NOT EXISTS ix_link_meeting ON meeting_link(meeting_id);

-- 一场会可以在多个文件夹
CREATE TABLE IF NOT EXISTS meeting_folder (
  meeting_id INTEGER NOT NULL,
  folder TEXT NOT NULL,
  PRIMARY KEY (meeting_id, folder)
);
CREATE INDEX IF NOT EXISTS ix_folder ON meeting_folder(folder);

CREATE TABLE IF NOT EXISTS import_batch (
  id SERIAL PRIMARY KEY,
  filename TEXT,
  uploaded_by TEXT,
  status TEXT DEFAULT 'running',   -- running / done / failed
  stats TEXT,                      -- JSON
  progress TEXT,                   -- JSON：{done, total, step}
  error TEXT,
  created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
  finished_at TEXT
);

-- 来访过的人；role 为空 = 按部门自动（董事局看全部，其他人只看研发市场销售）
CREATE TABLE IF NOT EXISTS member (
  user_id TEXT PRIMARY KEY,
  name TEXT,
  dept TEXT,
  role TEXT,                       -- admin 管理员 / all 全部可见 / biz 只看研发市场销售 / none 不能看
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS setting (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id SERIAL PRIMARY KEY,
  user_name TEXT,
  action TEXT,
  detail TEXT,
  created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

-- 检索用的文本段（摘要、原文切成小段）；向量存在 DATA_DIR/vectors.npz（按 id 对应）
CREATE TABLE IF NOT EXISTS chunk (
  id SERIAL PRIMARY KEY,
  meeting_id INTEGER NOT NULL,
  kind TEXT,                       -- summary 摘要 / transcript 原文 / material 会议材料（这时 meeting_id = -材料编号）
  seq INTEGER,
  text TEXT
);
CREATE INDEX IF NOT EXISTS ix_chunk_meeting ON chunk(meeting_id);

-- 提问记录（含 👍👎）
CREATE TABLE IF NOT EXISTS ask_log (
  id SERIAL PRIMARY KEY,
  user_id TEXT,
  user_name TEXT,
  scope TEXT,
  question TEXT,
  answer TEXT,
  sources TEXT,                    -- JSON
  rating INTEGER,                  -- 1 有用 / -1 没用
  comment TEXT,
  seconds DOUBLE PRECISION,
  created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);

-- 从会议里抽出来的信息：决议 / 风险 / 数字 / 客户产品等实体 / 话题（大模型抽取，周报月报和可视化用）
CREATE TABLE IF NOT EXISTS fact (
  id SERIAL PRIMARY KEY,
  meeting_id INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- decision 决议 / risk 风险问题 / metric 数字 / entity 实体 / topic 话题
  label TEXT,                      -- 实体类别（客户/产品/竞品/项目/区域）或数字的指标名
  text TEXT,                       -- 内容（实体名、话题名、决议原句…）
  value TEXT,                      -- 数字的值
  unit TEXT
);
CREATE INDEX IF NOT EXISTS ix_fact_meeting ON fact(meeting_id);
CREATE INDEX IF NOT EXISTS ix_fact_kind ON fact(kind, label);

-- 周报 / 月报
CREATE TABLE IF NOT EXISTS report (
  id SERIAL PRIMARY KEY,
  kind TEXT NOT NULL,              -- week / month
  scope TEXT NOT NULL,             -- all 全公司版（董事局、管理员）/ biz 研发市场销售版（部门负责人）
  period_start TEXT,
  period_end TEXT,
  title TEXT,
  content TEXT,                    -- Markdown
  stats TEXT,                      -- JSON：会议数、各文件夹数量、热门话题等（页面上画图用）
  created_by TEXT,
  created_at TEXT DEFAULT to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS ix_report ON report(kind, scope, period_start);

-- 同义词：语音识别错写（如 君瑶健康 → 均瑶健康），搜索和提问时当成一回事
CREATE TABLE IF NOT EXISTS synonym (
  id SERIAL PRIMARY KEY,
  canonical TEXT NOT NULL,         -- 标准写法
  variants TEXT,                   -- 其他写法，逗号分隔
  note TEXT,
  enabled INTEGER DEFAULT 1
);

-- 关键词修正（图谱、报告里把抽错类别的词改过来，或不显示）：name 是标准写法
CREATE TABLE IF NOT EXISTS term_rule (
  name TEXT PRIMARY KEY,
  action TEXT NOT NULL,            -- type：改类别（target=客户/产品/竞品/项目/区域/话题）；hide：不显示
  target TEXT,
  note TEXT,
  updated_at TEXT
);

-- 驾驶舱各卡片被点了多少次（持续迭代用：没人点的卡片该改或该删）
CREATE TABLE IF NOT EXISTS ui_event (
  day TEXT NOT NULL,
  card TEXT NOT NULL,
  action TEXT NOT NULL,            -- open 点开会议 / ask 追问 / view 打开驾驶舱 / period 切时间 等
  n INTEGER DEFAULT 0,
  PRIMARY KEY (day, card, action)
);

-- 会议材料（PDF）：挂到某场会（meeting_id）或放进某个文件夹（folder）；原文件在 DATA_DIR/materials/<id>.pdf
CREATE TABLE IF NOT EXISTS material (
  id SERIAL PRIMARY KEY,
  filename TEXT NOT NULL,
  sha256 TEXT UNIQUE,
  size BIGINT DEFAULT 0,
  pages INTEGER DEFAULT 0,
  title TEXT,
  doc_date TEXT,                   -- 从文件名 / 首页认出来的日期
  meeting_id INTEGER,
  folder TEXT,
  sensitive INTEGER DEFAULT 0,
  match_status TEXT DEFAULT 'pending',   -- pending 待核对 / confirmed 已确认（部门负责人只看已确认的）
  suggestions TEXT,                -- 建议挂的会议 [{id,title,date,score}]
  status TEXT DEFAULT 'queued',    -- queued / processing / ok / failed
  progress INTEGER DEFAULT 0,
  error TEXT,
  ocr_pages INTEGER DEFAULT 0,
  uploaded_by TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_material_meeting ON material(meeting_id);
CREATE TABLE IF NOT EXISTS material_page (
  material_id INTEGER NOT NULL,
  page_no INTEGER NOT NULL,
  text TEXT,
  source TEXT,                     -- text 文字层 / ocr 图片识别 / text+ocr / image 纯图片没识别
  PRIMARY KEY (material_id, page_no)
);

-- 大模型用量（按天、按用途）
CREATE TABLE IF NOT EXISTS llm_usage (
  day TEXT NOT NULL,
  task TEXT NOT NULL,              -- classify 分类 / extract 抽取 / ask 提问 / report 报告 / test 测试
  calls INTEGER DEFAULT 0,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  PRIMARY KEY (day, task)
);
