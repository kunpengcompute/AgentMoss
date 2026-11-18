"""SQLite database initialization and migration for AgentGate.

Manages connection lifecycle and schema creation.
Uses a single-write connection + WAL mode for concurrent reads.
"""

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from ..config import get_config

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    agent_type      TEXT NOT NULL,
    agent_version   TEXT DEFAULT '',
    hostname        TEXT NOT NULL,
    registered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    cwd             TEXT DEFAULT '',
    user_prompt     TEXT DEFAULT '',
    os_type         TEXT DEFAULT '',
    status          TEXT DEFAULT 'active',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at        TIMESTAMP,
    tool_call_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    seq             INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    command         TEXT NOT NULL,
    description     TEXT DEFAULT '',
    cwd             TEXT DEFAULT '',
    action_type     TEXT NOT NULL,
    action_detail   TEXT NOT NULL,
    decision        TEXT NOT NULL,
    risk_level      TEXT DEFAULT '',
    risk_type       TEXT DEFAULT '',
    violated_layers TEXT DEFAULT '[]',
    reason          TEXT DEFAULT '',
    confidence      INTEGER DEFAULT 0,
    analysis_duration_ms REAL DEFAULT 0,
    moss_response   TEXT DEFAULT '{}',
    called_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS system_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    tool_call_id    INTEGER REFERENCES tool_calls(id),
    os_info         TEXT DEFAULT '{}',
    resource_info   TEXT DEFAULT '{}',
    process_info    TEXT DEFAULT '{}',
    network_info    TEXT DEFAULT '{}',
    user_info       TEXT DEFAULT '{}',
    env_info        TEXT DEFAULT '{}',
    captured_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session_seq
    ON tool_calls(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_tool_calls_decision
    ON tool_calls(decision, called_at);
CREATE INDEX IF NOT EXISTS idx_sessions_agent_status
    ON sessions(agent_id, status);
"""


class Database:
    """Thread-safe SQLite database manager for AgentGate."""

    def __init__(self, db_path: Optional[str] = None):
        from ..config import resolve_storage_db_path
        # "auto" (the config default) is a sentinel, not a filename — see
        # resolve_storage_db_path. Treating it as a path used to litter the
        # CWD with a file named `auto`.
        self._db_path = resolve_storage_db_path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def init_schema(self) -> None:
        """Create tables and indexes (idempotent)."""
        conn = self._get_connection()
        with self._write_lock:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a read-only query. Returns cursor for iteration."""
        conn = self._get_connection()
        return conn.execute(sql, params)

    def execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write query under the write lock."""
        conn = self._get_connection()
        with self._write_lock:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def execute_many(self, sql: str, params_list: list[tuple]) -> sqlite3.Cursor:
        """Execute many writes under the write lock."""
        conn = self._get_connection()
        with self._write_lock:
            cursor = conn.executemany(sql, params_list)
            conn.commit()
            return cursor

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    @property
    def db_path(self) -> str:
        return str(self._db_path)


# Module-level singleton
_db: Optional[Database] = None


def get_db(db_path: Optional[str] = None) -> Database:
    global _db
    if _db is None:
        _db = Database(db_path)
        _db.init_schema()
    return _db


def get_agent_gate_db_path() -> str:
    """当前 AgentGate 存储路径（供 AgentMoss ``/api/v1/review`` 直读）。

    与 TS 版 ``getAgentGateDbPath()`` 对应：AgentMoss 的 review 接口拿到这个
    路径后**直接读文件**，把全量 tool_calls seed 进状态库 —— 数据不双份存储，
    全量事件流仍以 AgentGate 存储为唯一事实源。

    不复用 ``get_db()``：那会顺带建表/建目录，而调用方（低频回顾触发点）
    只需要一个路径字符串，不该有初始化副作用。
    """
    from ..config import resolve_storage_db_path

    return str(resolve_storage_db_path())
