from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pulse = _load("external_game_pulse", "AGENT-TEAM/scripts/external_game_pulse.py")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta_decks (snapshot_at TEXT, source_url TEXT)")
    return conn


def test_source_manifest_has_distinct_reviewed_evidence_tiers():
    sources = pulse.load_sources()

    assert {source["tier"] for source in sources} == {
        "official",
        "competitive_aggregate",
        "community_sentiment",
    }
    assert all(source["url"].startswith("https://") for source in sources)
    assert "do not scrape" in next(
        source["interpretation"] for source in sources if source["id"] == "clashroyale-community"
    )


def test_external_pulse_marks_old_meta_snapshot_for_review_without_writing():
    conn = _conn()
    conn.executemany(
        "INSERT INTO meta_decks VALUES (?, ?)",
        [
            ("2026-08-01T00:00:00Z", "https://example.test/a"),
            ("2026-08-01T00:00:00Z", "https://example.test/b"),
        ],
    )

    result = pulse.audit(
        conn,
        sources=pulse.load_sources(),
        max_meta_age_hours=168,
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )

    assert result["meta_snapshot"] == {
        "state": "review_due",
        "snapshot_at": "2026-08-01T00:00:00Z",
        "age_hours": 216.0,
        "deck_count": 2,
        "source_count": 2,
    }
    assert "do not scrape" in result["next_action"]


def test_external_pulse_handles_fresh_or_missing_meta_snapshot():
    conn = _conn()
    now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)

    missing = pulse.audit(conn, sources=pulse.load_sources(), now=now)
    assert missing["meta_snapshot"]["state"] == "missing"

    conn.execute("INSERT INTO meta_decks VALUES (?, ?)", ("2026-08-10T10:00:00Z", "https://x"))
    fresh = pulse.audit(conn, sources=pulse.load_sources(), now=now)
    assert fresh["meta_snapshot"]["state"] == "fresh"
    assert fresh["meta_snapshot"]["age_hours"] == 2.0


def test_source_manifest_rejects_missing_evidence_tier(tmp_path):
    path = tmp_path / "sources.toml"
    path.write_text(
        "version = 1\n[[source]]\nid = 'official'\ntier = 'official'\ncadence = 'weekly'\n"
        "url = 'https://example.test'\npurpose = 'facts'\ninterpretation = 'facts'\n"
    )

    with pytest.raises(ValueError, match="misses tiers"):
        pulse.load_sources(path)
