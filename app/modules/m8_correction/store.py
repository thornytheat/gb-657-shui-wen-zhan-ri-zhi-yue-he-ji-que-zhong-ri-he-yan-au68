"""M8 日值更正发布存储层。

与 M1–M7 共用一个 SQLite 文件，但表名全部以 corr_ 前缀独立，互不影响。
设计要点：
- corr_daily_values 只追加不更新、不删除，原始行与更正理由永久留痕；
- 每次发布生成一条不可变世代 corr_publishes，月量按世代冻结，切换只改指针；
- 发布在 BEGIN IMMEDIATE 事务里复核基准世代与发布锁，拒绝过期/并发更正。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

LOCK_KEY = "publish_lock"

SCHEMA = """
CREATE TABLE IF NOT EXISTS corr_runs (
    run_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    reason TEXT NOT NULL,
    base_gen TEXT,
    status TEXT NOT NULL,
    input_count INTEGER NOT NULL DEFAULT 0,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    isolated_count INTEGER NOT NULL DEFAULT 0,
    zero_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    checkpoints_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    pending_gen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS corr_daily_values (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    lineno INTEGER NOT NULL,
    station TEXT NOT NULL,
    date TEXT NOT NULL,
    version INTEGER,
    value_text TEXT NOT NULL,
    value_norm TEXT,
    kind TEXT NOT NULL,
    row_status TEXT NOT NULL,
    reason_code TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corr_daily_day
    ON corr_daily_values(station, date);
CREATE INDEX IF NOT EXISTS idx_corr_daily_run ON corr_daily_values(run_id);
CREATE TABLE IF NOT EXISTS corr_publishes (
    gen_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    base_gen TEXT,
    reason TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corr_day_versions (
    gen_id TEXT NOT NULL,
    station TEXT NOT NULL,
    date TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value_norm TEXT,
    source_row_id INTEGER NOT NULL,
    source_run_id TEXT NOT NULL,
    PRIMARY KEY (gen_id, station, date)
);
CREATE TABLE IF NOT EXISTS corr_day_conflicts (
    gen_id TEXT NOT NULL,
    station TEXT NOT NULL,
    date TEXT NOT NULL,
    version INTEGER NOT NULL,
    rows_json TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    PRIMARY KEY (gen_id, station, date)
);
CREATE TABLE IF NOT EXISTS corr_monthly (
    gen_id TEXT NOT NULL,
    station TEXT NOT NULL,
    month TEXT NOT NULL,
    rainfall_mm TEXT NOT NULL,
    observed_days INTEGER NOT NULL,
    zero_days INTEGER NOT NULL,
    missing_reports INTEGER NOT NULL DEFAULT 0,
    missing_days INTEGER NOT NULL,
    isolated_days INTEGER NOT NULL,
    expected_days INTEGER NOT NULL,
    missing_dates_json TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (gen_id, station, month)
);
CREATE TABLE IF NOT EXISTS corr_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dec(text):
    return None if text is None else Decimal(str(text))


class CorrectionStore:
    """更正域仓储；构造时幂等建表，老库直接升级不丢数据。"""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in
                       conn.execute("PRAGMA table_info(corr_runs)").fetchall()}
            if "pending_gen" not in columns:
                conn.execute("ALTER TABLE corr_runs ADD COLUMN pending_gen TEXT")
            month_cols = {row["name"] for row in
                          conn.execute("PRAGMA table_info(corr_monthly)").fetchall()}
            if "missing_reports" not in month_cols:
                conn.execute(
                    "ALTER TABLE corr_monthly ADD COLUMN missing_reports "
                    "INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def immediate(self):
        """写事务：立即拿库级写锁，把并发发布挡在入口。"""
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 状态指针 ----------
    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM corr_state WHERE key=?",
                               (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, conn, key: str, value: str | None) -> None:
        if value is None:
            conn.execute("DELETE FROM corr_state WHERE key=?", (key,))
            return
        conn.execute(
            "INSERT INTO corr_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def current_gen(self) -> str | None:
        return self.get_state("current_gen")

    def reset_stale_lock(self) -> bool:
        """进程启动后调用一次：清掉上次进程崩溃残留的锁值。

        SQLite 写锁随旧连接关闭已经释放，corr_state 里剩下的只是指针。
        返回是否清理过；调用方应只在进程/服务首次初始化时调用一次，
        运行期的并发拦截由发布事务内复查完成，不能反复清锁。
        """
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM corr_state WHERE key=?",
                               (LOCK_KEY,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM corr_state WHERE key=?", (LOCK_KEY,))
            return True

    # ---------- 更正任务 ----------
    def insert_run(self, conn, run: dict) -> None:
        conn.execute(
            """INSERT INTO corr_runs (run_id,source_name,source_digest,reason,base_gen,
                    status,input_count,checkpoints_json,note,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run["run_id"], run["source_name"], run["source_digest"], run["reason"],
             run.get("base_gen"), run["status"], run["input_count"],
             json.dumps(run.get("checkpoints", []), ensure_ascii=False),
             run.get("note", ""), run["created_at"], run["updated_at"]))

    def update_run(self, run_id: str, **fields) -> None:
        if not fields:
            return
        if "checkpoints" in fields:
            fields["checkpoints_json"] = json.dumps(
                fields.pop("checkpoints"), ensure_ascii=False)
        keys = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE corr_runs SET {keys} WHERE run_id=?",
                         (*fields.values(), run_id))

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM corr_runs WHERE run_id=?",
                               (run_id,)).fetchone()
        return self._run_row(row)

    def find_run_by_digest(self, source_digest: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM corr_runs WHERE source_digest=? "
                "ORDER BY created_at DESC LIMIT 1", (source_digest,)).fetchone()
        return self._run_row(row)

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corr_runs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._run_row(r) for r in rows]

    def _run_row(self, row) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item["checkpoints"] = json.loads(item.pop("checkpoints_json") or "[]")
        return item

    # ---------- 原始日值（只追加） ----------
    def insert_daily(self, conn, rows: list[dict], stamp: str) -> None:
        conn.executemany(
            """INSERT INTO corr_daily_values
                (run_id,lineno,station,date,version,value_text,value_norm,kind,
                 row_status,reason_code,note,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(r["run_id"], r["lineno"], r["station"], r["date"], r.get("version"),
              r["value_text"],
              None if r.get("value_norm") is None else str(r["value_norm"]),
              r["kind"], r["row_status"], r.get("reason_code"),
              r.get("note", ""), stamp) for r in rows])

    def load_all_valid_daily(self, conn) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM corr_daily_values WHERE row_status != 'isolated' "
            "ORDER BY station, date, row_id").fetchall()
        return [self._daily_row(r) for r in rows]

    def load_run_daily(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corr_daily_values WHERE run_id=? ORDER BY lineno",
                (run_id,)).fetchall()
        return [self._daily_row(r) for r in rows]

    def _daily_row(self, row) -> dict:
        item = dict(row)
        item["value_norm"] = _dec(item["value_norm"])
        return item

    # ---------- 世代产物 ----------
    def delete_gen_products(self, conn, gen_id: str) -> None:
        """续跑前清掉上一轮未激活的半成品，原始日值不动。"""
        for table in ("corr_day_versions", "corr_day_conflicts", "corr_monthly"):
            conn.execute(f"DELETE FROM {table} WHERE gen_id=?", (gen_id,))
        conn.execute("DELETE FROM corr_publishes WHERE gen_id=?", (gen_id,))

    def insert_publish(self, conn, gen: dict) -> None:
        conn.execute(
            """INSERT INTO corr_publishes (gen_id,run_id,base_gen,reason,
                    source_digest,status,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (gen["gen_id"], gen["run_id"], gen.get("base_gen"), gen["reason"],
             gen["source_digest"], gen["status"], gen["created_at"]))

    def get_publish(self, gen_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM corr_publishes WHERE gen_id=?",
                               (gen_id,)).fetchone()
        return dict(row) if row else None

    def list_publishes(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corr_publishes ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def insert_day_versions(self, conn, gen_id: str, rows: list[dict]) -> None:
        conn.executemany(
            """INSERT INTO corr_day_versions (gen_id,station,date,version,kind,
                    value_norm,source_row_id,source_run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(gen_id, r["station"], r["date"], r["version"], r["kind"],
              None if r.get("value_norm") is None else str(r["value_norm"]),
              r["source_row_id"], r["source_run_id"]) for r in rows])

    def insert_conflicts(self, conn, gen_id: str, rows: list[dict]) -> None:
        conn.executemany(
            """INSERT INTO corr_day_conflicts (gen_id,station,date,version,
                    rows_json,reason_code) VALUES (?,?,?,?,?,?)""",
            [(gen_id, r["station"], r["date"], r["version"],
              json.dumps(r["rows"], ensure_ascii=False, default=str),
              r["reason_code"]) for r in rows])

    def insert_monthly(self, conn, gen_id: str, rows: list[dict]) -> None:
        conn.executemany(
            """INSERT INTO corr_monthly (gen_id,station,month,rainfall_mm,
                    observed_days,zero_days,missing_reports,missing_days,isolated_days,
                    expected_days,missing_dates_json,state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(gen_id, r["station"], r["month"], str(r["rainfall_mm"]),
              r["observed_days"], r["zero_days"], r["missing_reports"],
              r["missing_days"], r["isolated_days"], r["expected_days"],
              json.dumps(r["missing_dates"], ensure_ascii=False), r["state"])
             for r in rows])

    def load_day_versions(self, gen_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corr_day_versions WHERE gen_id=? "
                "ORDER BY station, date", (gen_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["value_norm"] = _dec(item["value_norm"])
            out.append(item)
        return out

    def load_conflicts(self, gen_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corr_day_conflicts WHERE gen_id=? "
                "ORDER BY station, date", (gen_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["rows"] = json.loads(item.pop("rows_json"))
            out.append(item)
        return out

    def load_monthly(self, gen_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corr_monthly WHERE gen_id=? "
                "ORDER BY station, month", (gen_id,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["rainfall_mm"] = _dec(item["rainfall_mm"])
            item["missing_dates"] = json.loads(item.pop("missing_dates_json"))
            out.append(item)
        return out

    def mark_run_rows(self, conn, run_id: str, status_by_row: dict[int, str],
                      code_by_row: dict[int, str]) -> None:
        for row_id, status in status_by_row.items():
            conn.execute(
                "UPDATE corr_daily_values SET row_status=?, reason_code=? "
                "WHERE row_id=?",
                (status, code_by_row.get(row_id), row_id))
