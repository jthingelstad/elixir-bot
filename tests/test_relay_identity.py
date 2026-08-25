"""Cross-producer relay identities follow domain moments, not copy."""

from runtime.relay_identity import awareness_relay_identity, war_week_relay_key


def test_war_week_key_unifies_early_finish_and_final_close():
    assert war_week_relay_key(["race_finished:135:2"]) == "war-week:135:2"
    assert war_week_relay_key(["week_finished:135:2"]) == "war-week:135:2"
    assert war_week_relay_key(["war_day_opened:135:2:3", "race_finished:135:2"]) == "war-week:135:2"


def test_war_week_key_fails_open_for_multiple_weeks():
    assert war_week_relay_key(["week_finished:135:1", "week_finished:135:2"]) is None


def test_war_week_key_accepts_only_same_boundary_context():
    assert (
        war_week_relay_key(
            [
                "week_finished:135:3",
                "season_closed:135",
                "clan_league_changed:135",
            ]
        )
        == "war-week:135:3"
    )
    assert war_week_relay_key(["week_finished:135:2", "arena_changed:#AAA:1"]) is None


def test_awareness_relay_identity_is_stable_for_the_topic():
    assert awareness_relay_identity("war-week:135:2") == awareness_relay_identity("war-week:135:2")
