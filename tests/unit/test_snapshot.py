"""The baked-in demo snapshot: written compactly, restored only into emptiness.

A deployment on an ephemeral filesystem boots from the snapshot; one with a
persistent disk must never have its live history overwritten by it.
"""

from __future__ import annotations

import argparse
import sqlite3

import ds
import pytest

from datasentry_core.config import reset_settings


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    live = tmp_path / "live"
    monkeypatch.setenv("DS_DATA_DIR", str(live))
    monkeypatch.setenv("DS_DB_PATH", str(live / "datasentry.db"))
    monkeypatch.setenv("DS_WAREHOUSE_PATH", str(live / "warehouse.db"))
    reset_settings()
    yield live
    reset_settings()


def _write_db(path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        conn.execute("DELETE FROM t")
        conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.close()


def _read_db(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT v FROM t").fetchone()[0]
    finally:
        conn.close()


def test_snapshot_then_restore_into_empty_dir(data_env, tmp_path, monkeypatch):
    _write_db(data_env / "datasentry.db", "seeded")
    snap = tmp_path / "snap"
    assert ds.cmd_snapshot(argparse.Namespace(out=str(snap))) == 0
    assert (snap / "datasentry.db").is_file()

    (data_env / "datasentry.db").unlink()
    monkeypatch.setenv("DS_SNAPSHOT_DIR", str(snap))
    assert ds._restore_snapshot() is True
    assert _read_db(data_env / "datasentry.db") == "seeded"


def test_restore_never_overwrites_live_state(data_env, tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    _write_db(snap / "datasentry.db", "snapshot")
    _write_db(data_env / "datasentry.db", "live")
    monkeypatch.setenv("DS_SNAPSHOT_DIR", str(snap))
    assert ds._restore_snapshot() is False
    assert _read_db(data_env / "datasentry.db") == "live"


def test_restore_is_a_no_op_without_a_snapshot(data_env, monkeypatch):
    monkeypatch.delenv("DS_SNAPSHOT_DIR", raising=False)
    assert ds._restore_snapshot() is False


def test_snapshot_refuses_an_unseeded_clone(data_env, tmp_path):
    assert ds.cmd_snapshot(argparse.Namespace(out=str(tmp_path / "snap"))) == 1
