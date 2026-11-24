-- SQLite（自己电脑上试用）。服务器上用 schema_pg.sql，两份要保持一致
CREATE TABLE IF NOT EXISTS meeting (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  confidence REAL,
  reason TEXT,
  classified_by TEXT,              -- rule / llm / manual
  batch_id INTEGER,
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT,
  uploaded_by TEXT,
  status TEXT DEFAULT 'running',   -- running / done / failed
  stats TEXT,                      -- JSON
  progress TEXT,                   -- JSON：{done, total, step}
  error TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime')),
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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_name TEXT,
  action TEXT,
  detail TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
