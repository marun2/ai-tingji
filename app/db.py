"""数据库连接：服务器上用中台共用的 PostgreSQL，自己电脑上试用时用 SQLite（不用装数据库）。

- 设置了环境变量 DATABASE_URL（postgresql://…，中台部署时自动注入）→ PostgreSQL，
  表建在本应用自己的 schema 里（账号的 search_path 已经指向它）
- 没有 DATABASE_URL → SQLite，文件在 DATA_DIR/tingji.db

业务代码统一用 ? 作占位符、用 NOW 表示当前时间，返回的每一行都是 dict，
日期和时间统一转成字符串（'2026-10-19'、'2026-10-19 18:02:00'），两种数据库的结果完全一样。
"""
import datetime as dt
import decimal
import os
import pathlib
import re
import sqlite3
import threading
from contextlib import contextmanager

BASE = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", BASE.parent / "data"))
DB_PATH = DATA_DIR / "tingji.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
BACKEND = "PostgreSQL" if IS_PG else "SQLite"
# 当前时间（本地时区），写 SQL 时用 f"... updated_at={db.NOW} ..."
NOW = "now()" if IS_PG else "datetime('now','localtime')"

_lock = threading.RLock()
_conn = None


def _norm(v):
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _row(r) -> dict:
    return {k: _norm(v) for k, v in (r.items() if isinstance(r, dict) else zip(r.keys(), tuple(r)))}


_Q = re.compile(r"\?")


def _pg_sql(sql: str) -> str:
    """? → %s；原本的 % 要转义成 %%（psycopg 的规则）"""
    return _Q.sub("%s", sql.replace("%", "%%"))


class Result:
    def __init__(self, rows, rowcount):
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class Cursor:
    """事务里用的游标：execute 返回 Result（fetchone / fetchall 都是 dict）"""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, params=()):
        if IS_PG:
            cur = self.raw.cursor()
            cur.execute(_pg_sql(sql), tuple(params))
            rows = [_row(r) for r in cur.fetchall()] if cur.description else []
            return Result(rows, cur.rowcount)
        cur = self.raw.execute(sql, tuple(params))
        rows = [_row(r) for r in cur.fetchall()] if cur.description else []
        return Result(rows, cur.rowcount)

    def executemany(self, sql: str, seq):
        seq = list(seq)
        if not seq:
            return
        if IS_PG:
            with self.raw.cursor() as cur:
                cur.executemany(_pg_sql(sql), seq)
        else:
            self.raw.executemany(sql, seq)


def _connect():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    return c


def _raw():
    global _conn
    if _conn is None or (IS_PG and _conn.closed):
        _conn = _connect()
    return _conn


@contextmanager
def tx():
    """写操作统一走事务；单连接，用锁串行化（这个应用的并发很低）"""
    with _lock:
        c = _raw()
        if IS_PG:
            with c.transaction():
                yield Cursor(c)
        else:
            c.execute("BEGIN")
            try:
                yield Cursor(c)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        try:
            return Cursor(_raw()).execute(sql, params).fetchall()
        except Exception as e:
            # 数据库重启后连接会断，重连一次
            if IS_PG and type(e).__name__ in ("OperationalError", "InterfaceError"):
                global _conn
                _conn = None
                return Cursor(_raw()).execute(sql, params).fetchall()
            raise


def q1(sql: str, params=()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def init():
    with _lock:
        if IS_PG:
            sql = re.sub(r"--[^\n]*", "", (BASE / "schema_pg.sql").read_text(encoding="utf-8"))
            c = _raw()
            with c.transaction():
                for stmt in (x.strip() for x in sql.split(";")):
                    if stmt:
                        c.execute(stmt)
        else:
            _raw().executescript((BASE / "schema.sql").read_text(encoding="utf-8"))
        _migrate()


# 后来加的字段：老库里补上（CREATE TABLE IF NOT EXISTS 不会给已有的表加列）
# 格式：(表, 列, SQLite 类型, PostgreSQL 类型)
ADDED_COLUMNS: list[tuple[str, str, str, str]] = [
    ("meeting", "extracted_at", "TEXT", "TEXT"),        # 抽取完成时间（空 = 还没抽）
]


def _migrate():
    c = _raw()
    if IS_PG:
        with c.transaction():
            for t, col, _lite, pg in ADDED_COLUMNS:
                c.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} {pg}")
    else:
        for t, col, lite, _pg in ADDED_COLUMNS:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()}
            if col not in have:
                c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {lite}")


def insert(cur: "Cursor", table: str, row: dict) -> int:
    """插入一行并返回自增 id（两种数据库通用）"""
    cols = list(row)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    if IS_PG:
        return cur.execute(sql + " RETURNING id", [row[c] for c in cols]).fetchone()["id"]
    cur.execute(sql, [row[c] for c in cols])
    return cur.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
