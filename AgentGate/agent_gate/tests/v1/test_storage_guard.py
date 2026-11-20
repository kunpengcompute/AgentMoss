"""存储治理（2026-10）：单条截断 / 磁盘降级 / 归档。"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from agent_gate.shared.storage.db import Database
from agent_gate.shared.storage.record_guard import (
    clip_text,
    degrade_record,
    disk_free_percent,
    prepare_record_for_storage,
)
from agent_gate.shared.storage.repository import Repository


def _record(**overrides):
    rec = {
        "tool_name": "bash",
        "command": "ls -la",
        "description": "",
        "cwd": "/tmp",
        "action_type": "bash",
        "action_detail": "ls -la",
    }
    rec.update(overrides)
    return rec


def _moss(decision="Allow"):
    return {
        "decision": decision,
        "risk_level": "low",
        "risk_type": "",
        "violated_layers": [],
        "reason": "",
        "confidence": 0,
        "analysis_duration_ms": 0,
    }


def test_clip_text_short_untouched():
    assert clip_text("ls -la", 1024) == "ls -la"


def test_clip_text_truncates_with_digest():
    long = "a" * 2000
    out = clip_text(long, 100)
    assert out.startswith("a" * 100)
    assert "[truncated 1900B sha256:" in out
    # 校验值稳定
    assert clip_text(long, 100) == out


def test_clip_text_zero_limit_no_truncation():
    long = "x" * 500
    assert clip_text(long, 0) == long


def test_disk_free_percent_bounds(tmp_path):
    pct = disk_free_percent(tmp_path / "x.db")
    assert 0 <= pct <= 100


def test_disk_free_percent_fallback():
    assert disk_free_percent("/nonexistent/path/xyz") == 100.0


def test_degrade_record_keeps_decision():
    out = degrade_record(_record(command="x" * 2000, description="d" * 100, reason="r" * 2000))
    assert "[degraded]" in out["command"]
    assert len(out["command"]) < 600
    assert out["description"] == ""
    assert len(out["reason"]) == 256


def test_prepare_record_truncates_oversize(tmp_path):
    rec, degraded = prepare_record_for_storage(_record(command="a" * (2 * 1024 * 1024)), tmp_path / "x.db")
    assert degraded is False
    assert "[truncated" in rec["command"]
    assert len(rec["command"]) < 1024 * 1024 + 100


def test_archive_old_records(tmp_path):
    db = Database(str(tmp_path / "agent_gate.db"))
    db.init_schema()
    repo = Repository(db)
    agent_id = repo.get_or_create_agent("opendesk", "host", "0.1.0")
    repo.get_or_create_session("s1", agent_id)
    for i in range(20):
        repo.insert_tool_call("s1", _record(command=f"cmd-{i}"), _moss())

    assert repo.get_storage_stats()["toolCalls"] == 20
    archived, file_path = repo.archive_old_records(keep=5)
    assert archived == 15
    assert os.path.exists(file_path)
    assert (os.stat(file_path).st_mode & 0o777) == 0o600

    # 主库只剩最近 5 条，且是最新的
    assert repo.get_storage_stats()["toolCalls"] == 5
    rows = repo.query_tool_calls(limit=10)
    assert sorted(r["command"] for r in rows) == ["cmd-15", "cmd-16", "cmd-17", "cmd-18", "cmd-19"]

    # 归档文件解压后 15 条，可读
    lines = gzip.decompress(Path(file_path).read_bytes()).decode("utf-8").strip().split("\n")
    assert len(lines) == 15
    assert "cmd-0" in lines[0]
    db.close()


def test_archive_noop_when_below_keep(tmp_path):
    db = Database(str(tmp_path / "agent_gate.db"))
    db.init_schema()
    repo = Repository(db)
    agent_id = repo.get_or_create_agent("opendesk", "host", "0.1.0")
    repo.get_or_create_session("s1", agent_id)
    repo.insert_tool_call("s1", _record(), _moss())
    archived, file_path = repo.archive_old_records(keep=100)
    assert archived == 0
    assert file_path == ""
    assert repo.get_storage_stats()["toolCalls"] == 1
    db.close()


import os  # noqa: E402  （放在末尾便于上方语义阅读）


def test_clip_text_enforces_utf8_byte_budget():
    value = "你" * 80
    out = clip_text(value, 100)
    assert out.startswith("你" * 33 + "…[truncated 141B sha256:")
    assert len(out.split("…[truncated", 1)[0].encode("utf-8")) <= 100


def test_degraded_unicode_fields_follow_byte_limits():
    out = degrade_record(_record(
        command="查" * 1000, action_detail="读" * 1000, reason="因" * 1000,
    ))
    assert len(out["command"].split("…[degraded]", 1)[0].encode()) <= 512
    assert len(out["action_detail"].encode()) <= 512
    assert len(out["reason"].encode()) <= 256


@pytest.mark.parametrize("degraded", [False, True])
def test_repository_guards_moss_reason_before_persisting(monkeypatch, tmp_path, degraded):
    from agent_gate.shared.storage import record_guard
    import json

    monkeypatch.setattr(record_guard, "should_degrade_writes", lambda _: degraded)
    db = Database(str(tmp_path / "guard.db"))
    db.init_schema()
    repo = Repository(db)
    aid = repo.get_or_create_agent("test", "host", "1.0")
    repo.get_or_create_session("guarded", aid)
    reason = "原因" * 3000
    repo.insert_tool_call(
        "guarded", _record(description="diagnostic context"),
        {**_moss(), "reason": reason},
    )
    row = repo.get_session_traces("guarded")[0]
    if degraded:
        assert len(row["reason"].encode()) <= 256
        assert row["description"] == ""
    else:
        assert "[truncated" in row["reason"]
        assert len(row["reason"].split("…[truncated", 1)[0].encode()) <= 4096
        assert row["description"] == "diagnostic context"
    # The original structured result remains available for auditing.
    assert json.loads(row["moss_response"])["reason"] == reason
    db.close()


def test_archive_keep_bytes_reduces_keep_count(tmp_path):
    """大内容场景：字节超限时保留条数要低于 keep。"""
    db = Database(str(tmp_path / "bytes.db"))
    db.init_schema()
    repo = Repository(db)
    agent_id = repo.get_or_create_agent("opendesk", "host", "0.1.0")
    repo.get_or_create_session("s1", agent_id)
    for i in range(20):
        repo.insert_tool_call("s1", _record(command=("x" * 2000) + f"-{i}"), _moss())

    archived, _ = repo.archive_old_records(keep=6, keep_bytes=1)
    assert archived > 0
    assert repo.get_storage_stats()["toolCalls"] < 6
    db.close()


def test_cleanup_archives_by_size_and_age_disabled(tmp_path):
    """默认只按大小清理；max_age_days=0 不按时间删除。"""
    db = Database(str(tmp_path / "cleanup.db"))
    db.init_schema()
    repo = Repository(db)
    archive_dir = repo.archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "old.jsonl.gz").write_bytes(b"x" * 1024)
    (archive_dir / "new.jsonl.gz").write_bytes(b"y" * 1024)

    # 不按时间删：即使文件很老也保留
    old = archive_dir / "old.jsonl.gz"
    os.utime(old, (1, 1))
    cleanup = repo.cleanup_archives(max_bytes=1_000_000, max_age_days=0)
    assert cleanup["deleted"] == 0
    assert old.exists()

    # 按大小删：只留不超过 max_bytes 的最新文件
    cleanup = repo.cleanup_archives(max_bytes=1024, max_age_days=0)
    assert cleanup["deleted"] == 1
    assert cleanup["files"] == 1
    assert cleanup["bytes"] <= 1024
    db.close()
