"""Repository: CRUD operations for agents, sessions, tool_calls, and system_snapshots.

All methods use the thread-safe Database singleton for SQLite access.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .db import Database, get_db
from agent_gate.v1.instance_id import get_instance_id


class Repository:
    """Data access layer for AgentGate SQLite storage."""

    def __init__(self, db: Optional[Database] = None):
        self._db = db or get_db()

    # ---- Agent ----

    def get_or_create_agent(
        self,
        agent_type: str,
        hostname: str,
        agent_version: str = "",
    ) -> str:
        agent_id = f"{agent_type}:{hostname}"
        self._db.execute_write(
            """INSERT INTO agents (id, agent_type, agent_version, hostname, last_seen_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                   agent_version = excluded.agent_version,
                   last_seen_at = CURRENT_TIMESTAMP""",
            (agent_id, agent_type, agent_version, hostname),
        )
        return agent_id

    def update_last_seen(self, agent_id: str) -> None:
        self._db.execute_write(
            "UPDATE agents SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
            (agent_id,),
        )

    # ---- Session ----

    def get_or_create_session(
        self,
        session_id: str,
        agent_id: str,
        cwd: str = "",
        user_prompt: str = "",
        os_type: str = "",
    ) -> None:
        self._db.execute_write(
            """INSERT INTO sessions (id, agent_id, cwd, user_prompt, os_type, status)
               VALUES (?, ?, ?, ?, ?, 'active')
               ON CONFLICT(id) DO UPDATE SET
                   cwd = excluded.cwd,
                   user_prompt = excluded.user_prompt,
                   os_type = excluded.os_type""",
            (session_id, agent_id, cwd, user_prompt, os_type),
        )

    def close_session(self, session_id: str) -> None:
        self._db.execute_write(
            "UPDATE sessions SET status = 'closed', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )

    def get_session(self, session_id: str) -> Optional[dict]:
        cursor = self._db.execute(
            """SELECT s.*, a.agent_type, a.agent_version
               FROM sessions s JOIN agents a ON s.agent_id = a.id
               WHERE s.id = ?""",
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    # ---- Tool Calls ----

    def _get_next_seq(self, session_id: str) -> int:
        cursor = self._db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM tool_calls WHERE session_id = ?",
            (session_id,),
        )
        return cursor.fetchone()[0]

    def insert_tool_call(
        self,
        session_id: str,
        tool_call: dict,
        moss_result: dict,
    ) -> int:
        # 存储治理：单条截断 + 磁盘低水位降级（旧数据不动，只约束这一条）
        from .record_guard import prepare_record_for_storage

        tool_call, _degraded = prepare_record_for_storage(tool_call, self._db._db_path)
        # 在同一个 write lock 内原子化生成 seq 并写入，避免并发请求拿到相同 seq
        conn = self._db._get_connection()
        with self._db._write_lock:
            cursor = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM tool_calls WHERE session_id = ?",
                (session_id,),
            )
            seq = cursor.fetchone()[0]
            cursor = conn.execute(
                """INSERT INTO tool_calls
                   (session_id, seq, tool_name, command, description, cwd,
                    action_type, action_detail, decision, risk_level, risk_type,
                    violated_layers, reason, confidence, analysis_duration_ms, moss_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    seq,
                    tool_call.get("tool_name", ""),
                    tool_call.get("command", ""),
                    tool_call.get("description", ""),
                    tool_call.get("cwd", ""),
                    tool_call.get("action_type", ""),
                    tool_call.get("action_detail", ""),
                    moss_result.get("decision", "Deny"),
                    moss_result.get("risk_level", ""),
                    moss_result.get("risk_type", ""),
                    json.dumps(moss_result.get("violated_layers", [])),
                    moss_result.get("reason", ""),
                    moss_result.get("confidence", 0),
                    moss_result.get("analysis_duration_ms", 0),
                    json.dumps(moss_result, ensure_ascii=False),
                ),
            )
            conn.commit()
            tool_call_id = cursor.lastrowid

            # Update session counter
            conn.execute(
                "UPDATE sessions SET tool_call_count = tool_call_count + 1 WHERE id = ?",
                (session_id,),
            )
            conn.commit()

        return tool_call_id

    def get_action_history(self, session_id: str, limit: int = 100) -> list[dict]:
        # called_at 供 AgentMoss 的 P4 时间窗口（提权→外传）判定用；
        # 旧调用方没有这个字段时，AgentMoss 退回不设窗口的旧行为。
        cursor = self._db.execute(
            """SELECT action_type, action_detail, called_at
               FROM tool_calls
               WHERE session_id = ?
               ORDER BY seq ASC
               LIMIT ?""",
            (session_id, limit),
        )
        return [
            {
                "action_type": row["action_type"],
                "action_detail": row["action_detail"],
                "called_at": str(row["called_at"] or ""),
            }
            for row in cursor.fetchall()
        ]

    def get_recent_session_histories(
        self,
        agent_id: Optional[str] = None,
        exclude_session_id: Optional[str] = None,
        limit: int = 5,
        per_session_limit: int = 100,
    ) -> list[dict]:
        """取最近 N 个 session 的完整 action_history（跨会话，供 AgentMoss L2.4 形式化引擎）。

        返回形如 [{"session_id": "...", "action_history": [...]}, ...]，按 session
        最近活跃时间倒序；可排除当前 session（避免重复）并限定 agent。
        """
        conditions = ["t.id IS NOT NULL"]
        params: list = []
        if agent_id:
            conditions.append("s.agent_id = ?")
            params.append(agent_id)
        if exclude_session_id:
            conditions.append("s.id != ?")
            params.append(exclude_session_id)

        where_clause = " AND ".join(conditions)

        def _session_sql(extra_cond: str = "") -> str:
            cond = f"{where_clause} AND {extra_cond}" if extra_cond else where_clause
            return f"""SELECT s.id AS session_id, s.agent_id AS agent_key,
                              MAX(t.called_at) AS last_active
                       FROM tool_calls t
                       JOIN sessions s ON t.session_id = s.id
                       WHERE {cond}
                       GROUP BY s.id
                       ORDER BY last_active DESC"""

        # 组合攻击针对整台服务器：多 agent 时不能只取「全局最近 N 个会话」——
        # 高频 agent 会把别人的会话全挤出去。这里按 agent 分组各取一份再合并，
        # 保证每个来源都有历史进入上下文（与 TS 侧 buildSessionHistory 同语义）。
        agent_rows = self._db.execute(
            f"SELECT DISTINCT s.agent_id AS agent_key FROM sessions s "
            f"JOIN tool_calls t ON t.session_id = s.id WHERE {where_clause}",
            tuple(params),
        ).fetchall()
        agent_keys = [str(r["agent_key"]) for r in agent_rows if r["agent_key"]]
        rows: list[dict] = []
        if len(agent_keys) > 1:
            per_agent = max(1, limit // len(agent_keys))
            for key in agent_keys:
                cur = self._db.execute(
                    _session_sql("s.agent_id = ?") + " LIMIT ?",
                    tuple(params + [key, per_agent]),
                )
                rows.extend(dict(r) for r in cur.fetchall())
            rows.sort(key=lambda r: str(r.get("last_active") or ""), reverse=True)
            rows = rows[:limit]
        else:
            cur = self._db.execute(_session_sql() + " LIMIT ?", tuple(params + [limit]))
            rows.extend(dict(r) for r in cur.fetchall())

        # session → agent_type（来源，供 AgentMoss 记录在链路径里）
        agent_type_by_key = {
            str(r["agent_key"]): str(r["agent_type"])
            for r in self._db.execute(
                "SELECT id AS agent_key, agent_type FROM agents"
            ).fetchall()
        }

        result = []
        for row in rows:
            result.append({
                "session_id": row["session_id"],
                "agent_id": agent_type_by_key.get(str(row.get("agent_key") or ""), ""),
                "action_history": self.get_action_history(row["session_id"], per_session_limit),
                # 本库所有会话都属于本安装实例：展示层脱敏按此 ID 判断「同实例不
                # 隐藏」（对齐 TS buildSessionHistory 的 instance_id 注入）。
                "instance_id": get_instance_id(),
            })
        return result

    def get_session_traces(self, session_id: str) -> list[dict]:
        """Get full tool_call records for a session."""
        cursor = self._db.execute(
            """SELECT * FROM tool_calls
               WHERE session_id = ?
               ORDER BY seq ASC""",
            (session_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def query_tool_calls(
        self,
        agent_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        session_id: Optional[str] = None,
        decision: Optional[str] = None,
        risk_level: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict]:
        conditions = []
        params: list = []

        if session_id:
            conditions.append("t.session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("s.agent_id = ?")
            params.append(agent_id)
        if agent_type:
            # 逐 agent token 的隔离依据：agent_id 是 `<agent_type>:<hostname>`，
            # agent 只知道自己的类型、不知道（也不该知道）别人的 hostname。
            conditions.append("a.agent_type = ?")
            params.append(agent_type)
        if decision:
            conditions.append("t.decision = ?")
            params.append(decision)
        if risk_level:
            conditions.append("t.risk_level = ?")
            params.append(risk_level)
        if from_time:
            conditions.append("t.called_at >= ?")
            params.append(from_time)
        if to_time:
            conditions.append("t.called_at <= ?")
            params.append(to_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        sql = f"""SELECT t.* FROM tool_calls t
                  JOIN sessions s ON t.session_id = s.id
                  JOIN agents a ON s.agent_id = a.id
                  WHERE {where_clause}
                  ORDER BY t.called_at DESC
                  LIMIT ? OFFSET ?"""
        params.extend([limit, offset])

        cursor = self._db.execute(sql, tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def count_tool_calls(
        self,
        agent_id: Optional[str] = None,
        agent_type: Optional[str] = None,
        session_id: Optional[str] = None,
        decision: Optional[str] = None,
        risk_level: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
    ) -> int:
        """Return total count of tool_call records matching filters (no LIMIT/OFFSET)."""
        conditions = []
        params: list = []

        if session_id:
            conditions.append("t.session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("s.agent_id = ?")
            params.append(agent_id)
        if agent_type:
            conditions.append("a.agent_type = ?")
            params.append(agent_type)
        if decision:
            conditions.append("t.decision = ?")
            params.append(decision)
        if risk_level:
            conditions.append("t.risk_level = ?")
            params.append(risk_level)
        if from_time:
            conditions.append("t.called_at >= ?")
            params.append(from_time)
        if to_time:
            conditions.append("t.called_at <= ?")
            params.append(to_time)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        cursor = self._db.execute(
            f"""SELECT COUNT(*) as total FROM tool_calls t
                JOIN sessions s ON t.session_id = s.id
                JOIN agents a ON s.agent_id = a.id
                WHERE {where_clause}""",
            tuple(params),
        )
        row = cursor.fetchone()
        return row["total"] if row else 0

    def get_agents_list(self, agent_type: Optional[str] = None) -> list[dict]:
        """每个 agent 的会话数与调用数（`/api/v1/agents` 与看板 agents 面板）。

        与 TS 侧 `getAgentsList` 同构：按调用数降序，最多 20 条。
        """
        where = "WHERE a.agent_type = ?" if agent_type else ""
        params = (agent_type,) if agent_type else ()
        cursor = self._db.execute(
            f"""SELECT a.agent_type, a.hostname, a.agent_version AS version,
                       COUNT(DISTINCT s.id) AS session_count,
                       COUNT(t.id) AS call_count
                FROM agents a
                LEFT JOIN sessions s ON s.agent_id = a.id
                LEFT JOIN tool_calls t ON t.session_id = s.id
                {where}
                GROUP BY a.id
                ORDER BY call_count DESC
                LIMIT 20""",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_timeline(self, limit: int = 100) -> list[dict]:
        """按分钟聚合 Allow / Deny（`/api/v1/timeline` 与看板时间线）。"""
        cursor = self._db.execute(
            """SELECT strftime('%Y-%m-%dT%H:%M', called_at) AS time,
                      SUM(CASE WHEN decision = 'Allow' THEN 1 ELSE 0 END) AS allow,
                      SUM(CASE WHEN decision = 'Deny' THEN 1 ELSE 0 END) AS deny
               FROM tool_calls
               GROUP BY strftime('%Y-%m-%dT%H:%M', called_at)
               ORDER BY time DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_storage_stats(self) -> dict:
        """存储占用。WAL 模式下主库 + -wal + -shm 才是真实磁盘占用（与 TS 一致）。"""
        row = self._db.execute("SELECT COUNT(*) AS total FROM tool_calls").fetchone()
        db_bytes = 0
        try:
            p = self._db._db_path
            db_bytes = p.stat().st_size
            for suffix in ("-wal", "-shm"):
                sidecar = p.with_name(p.name + suffix)
                if sidecar.exists():
                    db_bytes += sidecar.stat().st_size
        except OSError:
            pass
        return {
            "dbBytes": db_bytes,
            "toolCalls": row["total"] if row else 0,
            "jsonFiles": 0,
            "jsonBytes": 0,
        }

    def clear_tool_calls(self) -> int:
        """删除全部 tool_call 并把会话计数归零（与 TS 的 clearToolCalls 一致）。

        sqlite 本连接启用了外键约束（PRAGMA foreign_keys=ON），而
        system_snapshots.tool_call_id → tool_calls(id) —— 直接删 tool_calls
        会抛 FOREIGN KEY constraint failed（xiaoO 实测 DELETE /tool_calls
        返回 500）。TS 侧 better-sqlite3 默认不启用外键所以没踩到。这里先清
        引用表再删主表，顺序满足外键约束。
        """
        self._db.execute_write("DELETE FROM system_snapshots")
        cursor = self._db.execute_write("DELETE FROM tool_calls")
        deleted = cursor.rowcount or 0
        self._db.execute_write("UPDATE sessions SET tool_call_count = 0")
        return deleted

    # ---- System Snapshots ----

    def insert_system_snapshot(
        self,
        session_id: str,
        tool_call_id: int,
        context: dict,
    ) -> None:
        self._db.execute_write(
            """INSERT INTO system_snapshots
               (session_id, tool_call_id, os_info, resource_info, process_info,
                network_info, user_info, env_info)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                tool_call_id,
                json.dumps(context.get("os", {}), ensure_ascii=False),
                json.dumps(context.get("resources", {}), ensure_ascii=False),
                json.dumps(context.get("process", {}), ensure_ascii=False),
                json.dumps(context.get("network", {}), ensure_ascii=False),
                json.dumps(context.get("user", {}), ensure_ascii=False),
                json.dumps(context.get("env", {}), ensure_ascii=False),
            ),
        )

    def get_latest_snapshot(self, session_id: str) -> Optional[dict]:
        cursor = self._db.execute(
            """SELECT * FROM system_snapshots
               WHERE session_id = ?
               ORDER BY captured_at DESC LIMIT 1""",
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    # ---- Cleanup ----

    def cleanup_old_records(self, retention_days: int = 30) -> int:
        """Delete records older than retention_days. Returns count of deleted rows."""
        cutoff = datetime.now(timezone.utc).isoformat()
        total = 0
        cursor = self._db.execute_write(
            "DELETE FROM system_snapshots WHERE captured_at < ?",
            (cutoff,),
        )
        total += cursor.rowcount
        cursor = self._db.execute_write(
            "DELETE FROM tool_calls WHERE called_at < ?",
            (cutoff,),
        )
        total += cursor.rowcount
        cursor = self._db.execute_write(
            "DELETE FROM sessions WHERE created_at < ? AND status = 'closed'",
            (cutoff,),
        )
        total += cursor.rowcount
        return total

    def archive_old_records(self, keep: int) -> tuple[int, str]:
        """把最老的记录归档到本地压缩文件，主库只保留最近 ``keep`` 条。

        归档文件是 **JSONL + gzip**（一行一条记录），放在 ``<数据库目录>/archive/``
        下，需要翻旧账时 ``zcat archive/xxx.jsonl.gz | grep ...`` 即可 —— 数据不丢，
        只是不在主库里了。写完文件并落盘后才删主库行，调用方可随后 ``vacuum()``。

        返回 ``(归档条数, 文件路径)``。
        """
        if keep <= 0:
            return 0, ""
        import gzip
        import json as _json
        from datetime import datetime as _dt

        conn = self._db._get_connection()
        with self._db._write_lock:
            total = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
            if total <= keep:
                return 0, ""
            row = conn.execute(
                "SELECT id FROM tool_calls ORDER BY id LIMIT 1 OFFSET ?",
                (total - keep - 1,),
            ).fetchone()
            if row is None:
                return 0, ""
            cutoff_id = row[0]
            rows = conn.execute(
                "SELECT * FROM tool_calls WHERE id <= ? ORDER BY id", (cutoff_id,)
            ).fetchall()
            if not rows:
                return 0, ""

            db_path = self._db._db_path
            archive_dir = db_path.parent / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = _dt.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
            file_path = archive_dir / f"tool_calls-{stamp}.jsonl.gz"
            payload = "\n".join(_json.dumps(dict(r), ensure_ascii=False, default=str) for r in rows) + "\n"
            # 0600：归档里同样是完整命令原文，不能跟随 umask 变成 0644
            import os as _os

            fd = _os.open(file_path, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
            with _os.fdopen(fd, "wb") as f:
                f.write(gzip.compress(payload.encode("utf-8")))

            # 文件落盘后才删主库
            cur = conn.execute("DELETE FROM tool_calls WHERE id <= ?", (cutoff_id,))
            deleted = cur.rowcount
            conn.execute(
                """UPDATE sessions SET tool_call_count = (
                       SELECT COUNT(*) FROM tool_calls WHERE tool_calls.session_id = sessions.id
                   )"""
            )
            conn.commit()
            return deleted, str(file_path)

    def vacuum(self) -> None:
        """真正释放磁盘空间：DELETE 后必须 VACUUM，否则文件不会变小。"""
        conn = self._db._get_connection()
        with self._db._write_lock:
            conn.execute("VACUUM")
