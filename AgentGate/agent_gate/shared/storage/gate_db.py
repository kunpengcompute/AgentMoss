"""AgentGate v2 — eBPF event storage (SQLite).

Separate from v1's agent_gate.db to avoid data duplication.
Stores: sessions, raw syscall events, trigger matches, enforcement actions.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gate_sessions (
    id              TEXT PRIMARY KEY,
    label           TEXT DEFAULT '',
    inferred_agent  TEXT DEFAULT '',
    main_pid        INTEGER DEFAULT 0,
    pid_tree        TEXT DEFAULT '[]',
    status          TEXT DEFAULT 'active',
    event_count     INTEGER DEFAULT 0,
    started_at      TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    ended_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gate_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    syscall         TEXT NOT NULL,
    pid             INTEGER NOT NULL,
    tid             INTEGER DEFAULT 0,
    ppid            INTEGER DEFAULT 0,
    uid             INTEGER DEFAULT 0,
    comm            TEXT DEFAULT '',
    timestamp_ns    INTEGER NOT NULL,
    target_path     TEXT DEFAULT '',
    target_addr     TEXT DEFAULT '',
    target_port     INTEGER DEFAULT 0,
    flags           TEXT DEFAULT '',
    exit_code       INTEGER DEFAULT 0,
    child_pid       INTEGER DEFAULT 0,
    raw_json        TEXT DEFAULT '{}',
    captured_at     TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS gate_trigger_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    event_pid       INTEGER DEFAULT 0,
    event_syscall   TEXT DEFAULT '',
    event_id        INTEGER DEFAULT 0,
    decision        TEXT DEFAULT '',
    detail          TEXT DEFAULT '',
    matched_at      TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS gate_enforcement_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT DEFAULT '',
    rule_name       TEXT DEFAULT '',
    action_type     TEXT NOT NULL,
    target_pid      INTEGER DEFAULT 0,
    success         INTEGER DEFAULT 1,
    detail          TEXT DEFAULT '',
    executed_at     TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_gate_events_session ON gate_events(session_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_gate_events_pid ON gate_events(pid);
CREATE INDEX IF NOT EXISTS idx_gate_events_type ON gate_events(event_type);
CREATE INDEX IF NOT EXISTS idx_gate_triggers_session ON gate_trigger_matches(session_id);

CREATE TABLE IF NOT EXISTS v3_proxy_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL DEFAULT 'tool_call',
    tool_name       TEXT NOT NULL,
    tool_call_id    TEXT DEFAULT '',
    arguments_raw   TEXT DEFAULT '{}',
    target_command  TEXT DEFAULT '',
    target_path     TEXT DEFAULT '',
    model           TEXT DEFAULT '',
    agent_type      TEXT DEFAULT '',
    tool_response   TEXT DEFAULT '{}',
    raw_json        TEXT DEFAULT '{}',
    decision        TEXT DEFAULT '',
    captured_at     TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_v3_proxy_session ON v3_proxy_events(session_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_v3_proxy_tool ON v3_proxy_events(tool_name);
CREATE INDEX IF NOT EXISTS idx_v3_proxy_captured ON v3_proxy_events(captured_at);
CREATE INDEX IF NOT EXISTS idx_v3_proxy_agent ON v3_proxy_events(agent_type);
"""


class GateDB:
    """Thread-safe SQLite database for AgentGate events."""

    def __init__(self, db_path: Optional[str] = None):
        from agent_gate.shared.config import resolve_storage_db_path
        # "auto" (the config default) is a sentinel, not a filename — see
        # resolve_storage_db_path. Treating it as a path used to litter the
        # CWD with a file named `auto`.
        self._db_path = resolve_storage_db_path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def init_schema(self) -> None:
        conn = self._get_connection()
        with self._write_lock:
            conn.executescript(_SCHEMA_SQL)
            # Migration: add new columns to gate_trigger_matches if missing
            try:
                conn.execute("ALTER TABLE gate_trigger_matches ADD COLUMN event_id INTEGER DEFAULT 0")
            except Exception:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE gate_trigger_matches ADD COLUMN decision TEXT DEFAULT ''")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE gate_trigger_matches ADD COLUMN action_taken TEXT DEFAULT ''")
            except Exception:
                pass
            conn.commit()

    def insert_session(self, session_dict: dict) -> None:
        sql = """INSERT OR REPLACE INTO gate_sessions
                 (id, label, inferred_agent, main_pid, pid_tree, status, event_count, started_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        import json
        params = (
            session_dict.get("session_id", session_dict.get("id", "")),
            session_dict.get("label", ""),
            session_dict.get("inferred_agent", session_dict.get("inferred_agent_type", "")),
            session_dict.get("main_pid", 0),
            json.dumps(session_dict.get("pid_tree", [])),
            session_dict.get("status", "active"),
            session_dict.get("event_count", 0),
            session_dict.get("first_event_at", session_dict.get("started_at", "")),
        )
        self._conn_write(sql, params)

    def insert_event(self, session_id: str, event: dict) -> int:
        sql = """INSERT INTO gate_events
                 (session_id, event_type, syscall, pid, tid, ppid, uid, comm,
                  timestamp_ns, target_path, target_addr, target_port, flags,
                  exit_code, child_pid, raw_json)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        import json
        action = event.get("action", {})
        process = event.get("process", {})
        target = event.get("target", {})
        params = (
            session_id,
            action.get("action_type", ""),
            action.get("syscall", ""),
            process.get("pid", 0),
            process.get("tid", 0),
            process.get("ppid", 0),
            process.get("uid", 0),
            process.get("comm", ""),
            event.get("timestamp", {}).get("timestamp_ns", 0),
            target.get("pathname", ""),
            target.get("remote_addr", ""),
            target.get("remote_port", 0),
            action.get("flags", ""),
            action.get("exit_code", 0),
            action.get("child_pid", 0),
            json.dumps(event, default=str),
        )
        return self._conn_write(sql, params)

    def insert_trigger_match(self, session_id: str, rule_name: str, severity: str,
                              event_pid: int, event_syscall: str, detail: str,
                              event_id: int = 0, decision: str = "",
                              action_taken: str = "") -> None:
        sql = """INSERT INTO gate_trigger_matches
                 (session_id, rule_name, severity, event_pid, event_syscall, detail, event_id, decision, action_taken)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        self._conn_write(sql, (session_id, rule_name, severity, event_pid, event_syscall, detail, event_id, decision, action_taken))

    def insert_enforcement(self, session_id: str, rule_name: str, action_type: str,
                           target_pid: int, success: bool, detail: str) -> None:
        sql = """INSERT INTO gate_enforcement_actions
                 (session_id, rule_name, action_type, target_pid, success, detail)
                 VALUES (?, ?, ?, ?, ?, ?)"""
        self._conn_write(sql, (session_id, rule_name, action_type, target_pid, int(success), detail))

    def update_trigger_decision(self, session_id: str, rule_name: str, decision: str,
                                event_id: int = 0, action_taken: str = "") -> None:
        if event_id:
            if action_taken:
                sql = """UPDATE gate_trigger_matches SET decision=?, action_taken=?
                         WHERE event_id=? AND (decision IS NULL OR decision = '')"""
                self._conn_write(sql, (decision, action_taken, event_id))
            else:
                sql = """UPDATE gate_trigger_matches SET decision=?
                         WHERE event_id=? AND (decision IS NULL OR decision = '')"""
                self._conn_write(sql, (decision, event_id))
        else:
            if action_taken:
                sql = """UPDATE gate_trigger_matches SET decision=?, action_taken=?
                         WHERE session_id=? AND rule_name=? AND (decision IS NULL OR decision = '')"""
                self._conn_write(sql, (decision, action_taken, session_id, rule_name))
            else:
                sql = """UPDATE gate_trigger_matches SET decision=?
                         WHERE session_id=? AND rule_name=? AND (decision IS NULL OR decision = '')"""
                self._conn_write(sql, (decision, session_id, rule_name))

    def update_session_ended(self, session_id: str) -> None:
        sql = "UPDATE gate_sessions SET status='ended', ended_at=datetime('now', 'localtime') WHERE id=?"
        self._conn_write(sql, (session_id,))

    def get_session_count(self) -> int:
        row = self._get_connection().execute("SELECT COUNT(*) FROM gate_sessions").fetchone()
        return row[0]

    def get_event_count(self) -> int:
        row = self._get_connection().execute("SELECT COUNT(*) FROM gate_events").fetchone()
        return row[0]

    # ===== v3 Proxy Events =====

    def insert_proxy_event(self, session_id: str, event: dict, decision: str = "") -> int:
        sql = """INSERT INTO v3_proxy_events
                 (session_id, event_type, tool_name, tool_call_id, arguments_raw,
                  target_command, target_path, model, agent_type, tool_response, raw_json, decision)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        import json
        # 超长 tool call（写文件内容）会把四个大字段原样写库，导致数据库膨胀。
        # 与 repository.tool_calls 表对齐：走 record_guard.clip_text，默认 1MB，
        # 截断后附 sha256 校验值保留可追溯性。
        # 注意：AgentMoss 判定用的是内存 session.events，不受此处截断影响。
        from .record_guard import clip_text
        action = event.get("action", {})
        target = event.get("target", {})
        payload = event.get("payload", {})
        params = (
            session_id,
            action.get("action_type", "tool_call"),
            action.get("tool_name", ""),
            action.get("tool_call_id", ""),
            clip_text(action.get("arguments_raw", "{}")),
            clip_text(target.get("command", "")),
            target.get("file_path", ""),
            payload.get("request_model", ""),
            event.get("agent_type", ""),
            clip_text(json.dumps(payload.get("tool_response_content", ""))),
            clip_text(json.dumps(event, default=str)),
            decision,
        )
        return self._conn_write(sql, params)

    def get_proxy_event_count(self) -> int:
        row = self._get_connection().execute("SELECT COUNT(*) FROM v3_proxy_events").fetchone()
        return row[0]

    def purge_session_events(self, session_id: str) -> int:
        """Delete all events belonging to a session. Returns count deleted."""
        sql = "DELETE FROM v3_proxy_events WHERE session_id = ?"
        conn = self._get_connection()
        with self._write_lock:
            cursor = conn.execute(sql, (session_id,))
            deleted = cursor.rowcount
            conn.commit()
            return deleted

    def _conn_write(self, sql: str, params: tuple = ()) -> int:
        conn = self._get_connection()
        with self._write_lock:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


# Module-level singleton
_gate_db: Optional[GateDB] = None


def get_gate_db(db_path: Optional[str] = None) -> GateDB:
    global _gate_db
    if _gate_db is None:
        _gate_db = GateDB(db_path)
        _gate_db.init_schema()
    return _gate_db
