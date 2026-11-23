"""Regression tests for ``storage.db_path`` resolution.

``DEFAULT_CONFIG`` ships ``storage.db_path = "auto"`` — a *sentinel* meaning
"pick the standard location".  ``_run_server`` handed that literal string to
the storage layer, which passed it to ``sqlite3.connect``: SQLite then created
a file literally named ``auto`` in the process CWD.  The consequences were
silent — the dashboard's history moved with the working directory, and a
restart from anywhere else looked like "all records lost".

An explicit path (CLI flag or config) must always win; ``None`` / ``""`` /
``"auto"`` must all resolve to the standard location.
"""

import argparse
import sqlite3
from pathlib import Path

import pytest

from agent_gate.shared.config import resolve_storage_db_path
from agent_gate.shared.storage.db import Database


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Keep path resolution — and any DB it creates — inside tmp_path."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SUDO_USER", raising=False)
    return tmp_path


def _run_from_clean_workdir(tmp_path, monkeypatch) -> Path:
    """Run from an empty CWD so a stray ``auto`` file is unambiguous."""
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def test_auto_sentinel_resolves_to_standard_location(isolated_home):
    resolved = resolve_storage_db_path("auto")

    assert resolved.is_absolute()
    assert resolved.name == "agent_gate.db"
    assert resolved.parent.name == "agent_gate"


def test_none_and_empty_resolve_like_auto(isolated_home):
    expected = resolve_storage_db_path("auto")

    assert resolve_storage_db_path(None) == expected
    assert resolve_storage_db_path("") == expected


def test_explicit_path_is_used_verbatim(isolated_home):
    explicit = isolated_home / "custom" / "mydb.sqlite"

    assert resolve_storage_db_path(str(explicit)) == explicit


def test_database_never_creates_a_file_named_auto(isolated_home, monkeypatch):
    workdir = _run_from_clean_workdir(isolated_home, monkeypatch)

    db = Database("auto")
    db.init_schema()

    assert not (workdir / "auto").exists()
    assert Path(db.db_path) == resolve_storage_db_path("auto")


def test_database_preserves_sqlite_memory_filename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database(":memory:")
    try:
        db.init_schema()
        assert db.execute("PRAGMA database_list").fetchone()["file"] == ""
        assert not (tmp_path / ":memory:").exists()
    finally:
        db.close()


def test_db_path_lookup_before_and_after_startup(isolated_home, monkeypatch):
    from agent_gate.shared.storage import db as db_module

    monkeypatch.setattr(db_module, "_db", None)
    default_path = resolve_storage_db_path("auto")
    assert Path(db_module.get_agent_gate_db_path()) == default_path
    assert not default_path.exists()
    active_path = isolated_home / "custom.sqlite"
    db = db_module.get_db(str(active_path))
    try:
        assert Path(db_module.get_agent_gate_db_path()).samefile(active_path)
    finally:
        db.close()


@pytest.mark.parametrize("filename", ["file:custom.sqlite?mode=rwc", "file:tmp?mode=memory"])
def test_database_preserves_sqlite_uri_interpretation(tmp_path, monkeypatch, filename):
    monkeypatch.chdir(tmp_path)
    # Whether SQLite recognizes URI filenames by default depends on its build.
    conn = sqlite3.connect(filename)
    try:
        expected = conn.execute("PRAGMA database_list").fetchone()[2]
    finally:
        conn.close()
    db = Database(filename)
    try:
        db.init_schema()
        assert db.execute("PRAGMA database_list").fetchone()["file"] == expected
        assert db.db_path == (expected or ":memory:")
    finally:
        db.close()


@pytest.mark.parametrize("source", ["default", "config", "env", "cli"])
def test_run_server_passes_configured_db_path(isolated_home, monkeypatch, source):
    """The bug lived in ``_run_server``, so guard its wiring too."""
    captured: dict = {}

    import agent_gate.shared.server.app as app_module

    monkeypatch.setattr(
        app_module, "run_server", lambda **kwargs: captured.update(kwargs)
    )

    from agent_gate.v1 import cli
    from agent_gate.shared import config

    monkeypatch.setattr(config, "_config", None)
    monkeypatch.delenv("AGENT_GATE_STORAGE_DB_PATH", raising=False)
    config_path = isolated_home / "config.yaml"
    expected = isolated_home / "share" / "agent_gate" / "agent_gate.db"
    if source != "default":
        expected = isolated_home / f"{source}.sqlite"
        config_path.write_text(f"storage:\n  db_path: {isolated_home / 'config.sqlite'}\n")
    if source in ("env", "cli"):
        monkeypatch.setenv("AGENT_GATE_STORAGE_DB_PATH", str(isolated_home / "env.sqlite"))

    args = argparse.Namespace(
        config_path=config_path if source != "default" else None,
        host=None,
        port=None,
        mode=None,
        socket_path=None,
        log_level=None,
        workers=None,
        db_path=str(expected) if source == "cli" else None,
    )
    cli._run_server(args)

    assert Path(captured["db_path"]) == expected
