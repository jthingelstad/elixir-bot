from __future__ import annotations

import sqlite3


def _seed_eval_fixture_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE members ("
            "player_tag TEXT PRIMARY KEY, current_name TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE war_period_clan_status ("
            "clan_tag TEXT, clan_name TEXT)"
        )
        conn.execute(
            "CREATE TABLE member_battle_facts ("
            "opponent_tag TEXT, opponent_name TEXT, opponent_clan_tag TEXT)"
        )
        conn.execute(
            "INSERT INTO members VALUES ('#AAA111', 'Eval Alice', 'active')"
        )
        conn.execute(
            "INSERT INTO war_period_clan_status VALUES ('#EXT999', 'Outside Clan')"
        )
        conn.execute(
            "INSERT INTO member_battle_facts VALUES ('#OPP222', 'Opponent Bob', '#EXT999')"
        )
        conn.commit()
    finally:
        conn.close()


def test_eval_all_requests_samples_from_canonical_db_path(tmp_path, monkeypatch):
    from scripts import eval_all_requests

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setattr(eval_all_requests.db, "DB_PATH", str(db_path), raising=False)

    fixtures = eval_all_requests.sample_real_tags()
    clan, _war = eval_all_requests._fake_clan_ctx()

    assert fixtures["our_member_tags"] == [("Eval Alice", "#AAA111")]
    assert fixtures["external_clan_tags"] == [("Outside Clan", "#EXT999")]
    assert fixtures["external_player_tags"] == [("Opponent Bob", "#OPP222")]
    assert clan["members"] == [{"tag": "#AAA111", "name": "Eval Alice"}]


def test_eval_card_conversations_builds_context_from_canonical_db_path(tmp_path, monkeypatch):
    from scripts import eval_card_conversations

    db_path = tmp_path / "elixir-v5-fixture.db"
    _seed_eval_fixture_db(db_path)
    monkeypatch.setattr(eval_card_conversations.db, "DB_PATH", str(db_path), raising=False)

    clan, _war = eval_card_conversations._build_clan_war_context()

    assert clan["members"] == [{"tag": "#AAA111", "name": "Eval Alice"}]
