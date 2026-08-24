from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


label_audit = _load("game_mode_label_audit", "scripts/audit_game_mode_labels.py")
acceptance = _load("natural_label_acceptance", "scripts/check_natural_label_acceptance.py")


def _sentinel_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE api_sentinel_observations (
            observation_id INTEGER PRIMARY KEY,
            sentinel_type TEXT NOT NULL,
            name TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            sample_json TEXT
        )"""
    )
    return conn


def test_game_mode_label_audit_distinguishes_curated_and_unreviewed_modes():
    conn = _sentinel_conn()
    now = datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)
    conn.executemany(
        "INSERT INTO api_sentinel_observations VALUES (?, ?, ?, ?, ?)",
        [
            (
                1,
                "battle_game_mode",
                "72000511",
                "2026-08-24T12:00:00Z",
                json.dumps({"name": "Crazy_Arena_SuddenDeath", "event_tag": "#A"}),
            ),
            (
                2,
                "battle_game_mode",
                "72999999",
                "2026-08-24T12:30:00Z",
                json.dumps({"name": "Future_Mode", "event_tag": "#B"}),
            ),
        ],
    )

    findings = label_audit.audit(conn, hours=48, now=now)

    assert findings[0]["label_status"] == "unreviewed"
    assert findings[0]["display_label"] == "Future"
    assert findings[1]["label_status"] == "curated"
    assert findings[1]["display_label"] == "C.H.A.O.S Sudden Death"


def _posts_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE awareness_posts (lane TEXT, posted_at TEXT, content_preview TEXT)")
    return conn


def test_natural_label_acceptance_waits_then_accepts_without_exposing_copy():
    conn = _posts_conn()
    start = "2026-08-24T12:29:40Z"
    now = datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)

    waiting = acceptance.check(
        conn, labels=["C.H.A.O.S Sudden Death"], since=start, expires_hours=336, now=now
    )
    assert waiting["state"] == "waiting"
    conn.execute(
        "INSERT INTO awareness_posts VALUES (?, ?, ?)",
        ("elixir", "2026-08-24T14:00:00Z", "The C.H.A.O.S Sudden Death event is live."),
    )
    accepted = acceptance.check(
        conn,
        labels=["C.H.A.O.S Sudden Death"],
        since=start,
        expires_hours=336,
        now=now,
    )
    assert accepted["state"] == "accepted"
    assert accepted["found_labels"] == ["C.H.A.O.S Sudden Death"]
    assert accepted["matches"] == [
        {
            "lane": "elixir",
            "posted_at": "2026-08-24T14:00:00Z",
            "labels": ["C.H.A.O.S Sudden Death"],
        }
    ]


def test_natural_label_acceptance_expires_without_manufacturing_a_post():
    result = acceptance.check(
        _posts_conn(),
        labels=["C.H.A.O.S Sudden Death"],
        since="2026-08-01T00:00:00Z",
        expires_hours=24,
        now=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert result["state"] == "expired"
