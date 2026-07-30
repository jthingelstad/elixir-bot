import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scripts import confidence_report, eval_post_quality


def _utc(hours_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _liveness_conn(
    *, thought_at: str, plan_json: str = "{}", chose_silence: int = 1, post_count: int = 0
):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE awareness_thoughts ("
        "at TEXT NOT NULL, plan_json TEXT, chose_silence INTEGER NOT NULL, post_count INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE leader_action_recommendations ("
        "status TEXT NOT NULL, copy_message_id TEXT, is_test INTEGER, proposed_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO awareness_thoughts VALUES (?, ?, ?, ?)",
        (thought_at, plan_json, chose_silence, post_count),
    )
    return conn


def test_liveness_accepts_recent_deliberate_silence(monkeypatch):
    conn = _liveness_conn(thought_at=_utc(1))
    monkeypatch.setattr("scripts.read_only_db.connect_read_only", lambda: conn)

    assert confidence_report._liveness() == []


def test_liveness_flags_stale_successful_awareness_decision(monkeypatch):
    conn = _liveness_conn(thought_at=_utc(15))
    monkeypatch.setattr("scripts.read_only_db.connect_read_only", lambda: conn)

    finding = confidence_report._liveness()[0]

    assert finding.startswith("no successful awareness decision in 15.0h")


def test_liveness_does_not_treat_failed_plan_as_a_success(monkeypatch):
    conn = _liveness_conn(
        thought_at=_utc(1),
        plan_json='{"_error": {"kind": "delivery"}}',
        chose_silence=0,
        post_count=1,
    )
    monkeypatch.setattr("scripts.read_only_db.connect_read_only", lambda: conn)

    assert confidence_report._liveness() == [
        "no successful awareness decision recorded — awareness may be silently stuck"
    ]


def test_liveness_retains_stuck_leader_action_alarm(monkeypatch):
    conn = _liveness_conn(thought_at=_utc(1))
    conn.execute(
        "INSERT INTO leader_action_recommendations VALUES (?, ?, ?, ?)",
        ("proposed", None, 0, _utc(3)),
    )
    monkeypatch.setattr("scripts.read_only_db.connect_read_only", lambda: conn)

    assert confidence_report._liveness() == [
        "1 leader-action(s) proposed >2h ago but never posted — card posting may be broken"
    ]


def test_quality_eval_is_read_only(monkeypatch):
    seen = {}

    def fake_run_eval(**kwargs):
        seen.update(kwargs)
        return {"flagged": [], "sampled": 0}

    monkeypatch.setattr(eval_post_quality, "run_eval", fake_run_eval)

    result = confidence_report._quality(days=3, quick=True)

    assert result["available"] is True
    assert seen == {"days": 3, "use_llm": False, "record_feedback": False}


def test_quality_pillar_error_is_a_finding():
    findings = confidence_report._finding_count(
        errors=[],
        liveness=[],
        tests={"ok": True},
        quality={"available": True, "error": "missing table"},
    )

    assert findings == 1


def test_unavailable_quality_pillar_is_a_finding():
    findings = confidence_report._finding_count(
        errors=[],
        liveness=[],
        tests={"ok": True},
        quality={"available": False, "reason": "import failed"},
    )

    assert findings == 1


def test_read_only_connection_refuses_writes(tmp_path, monkeypatch):
    path = tmp_path / "report.db"
    source = sqlite3.connect(path)
    source.execute("CREATE TABLE evidence (id INTEGER)")
    source.close()
    monkeypatch.setenv("ELIXIR_DB_PATH", str(path))

    conn = eval_post_quality.connect_read_only()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE agent_team_write_probe (id INTEGER)")
    finally:
        conn.close()
