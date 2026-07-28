import sqlite3

import pytest

from scripts import confidence_report, eval_post_quality


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
