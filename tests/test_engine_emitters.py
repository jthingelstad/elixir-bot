"""Golden-pair emitter tests (migration.md Phase 8): (previous_state,
observed_state) → exact event list. Spec: events.md §3–§4, architecture §8."""

from __future__ import annotations

import json

import pytest

from engine.change_sets import ChangeSetInvariantError, derive_roster_change_set
from engine.emitters import emit
from engine.emitters.clan import emit_calendar, project_clan_aspects
from engine.emitters.player import project_player_aspects

NOW = "2026-07-01T12:00:00Z"
LATER = "2026-07-01T13:00:00Z"
TAG = "#AAA111"


def _profile(level=44, wins=4990, best=6890, badges=None, arena=(54000012, "Spooky Town")):
    return {
        "tag": TAG,
        "name": "Alice",
        "expLevel": level,
        "wins": wins,
        "bestTrophies": best,
        "trophies": best - 100,
        "arena": {"id": arena[0], "name": arena[1]},
        "badges": [{"name": n, "level": lv} for n, lv in (badges or {}).items()],
    }


def _events(conn, table="player_events"):
    return [
        (r["event_type"], r["dedup_key"])
        for r in conn.execute(f"SELECT event_type, dedup_key FROM {table} ORDER BY event_id")
    ]


def _emit_profile(conn, payload, at):
    return emit(conn, "player", TAG, "profile", project_player_aspects(payload)["profile"], at)


def test_first_sight_emits_nothing(engine_conn):
    assert _emit_profile(engine_conn, _profile(), NOW) == 0
    assert _events(engine_conn) == []
    row = engine_conn.execute(
        "SELECT observed_at, prev_observed_at FROM state_baselines "
        "WHERE entity_kind='player' AND entity_tag=? AND aspect='profile'",
        (TAG,),
    ).fetchone()
    assert row["observed_at"] == NOW and row["prev_observed_at"] is None


def test_career_wins_golden_pair(engine_conn):
    # expLevel is retired (no level_up); career_wins_milestone is the profile-
    # aspect ladder event. Account progression now rides collection_level_
    # milestone (cards aspect) — see test_collection_level_milestone.py.
    _emit_profile(engine_conn, _profile(level=44, wins=4990), NOW)
    n = _emit_profile(engine_conn, _profile(level=45, wins=5100), LATER)
    events = _events(engine_conn)
    assert ("career_wins_milestone", f"career_wins_milestone:{TAG}:5000") in events
    assert all(e[0] != "level_up" for e in events)
    assert n == len(events)
    # timing honesty: estimated, window bounded by prev observation
    row = engine_conn.execute("SELECT timing, window_start FROM player_events LIMIT 1").fetchone()
    assert row["timing"] == "estimated" and row["window_start"] == NOW


def test_unchanged_payload_emits_nothing(engine_conn):
    _emit_profile(engine_conn, _profile(), NOW)
    assert _emit_profile(engine_conn, _profile(), LATER) == 0
    assert _events(engine_conn) == []


def test_best_trophies_peak_every_250(engine_conn):
    # Raised from 100 → 250 (2026-07-12) so tiny incremental peaks don't fire.
    _emit_profile(engine_conn, _profile(best=6890), NOW)
    _emit_profile(engine_conn, _profile(best=7010), LATER)
    events = _events(engine_conn)
    assert ("best_trophies_peak", f"best_trophies_peak:{TAG}:7000") in events
    # 6900 is no longer a boundary at step 250
    assert ("best_trophies_peak", f"best_trophies_peak:{TAG}:6900") not in events


def test_best_trophies_peak_ignores_small_increment(engine_conn):
    # The exact 24h-review case: 13,000 → 13,142 must NOT re-fire a peak.
    _emit_profile(engine_conn, _profile(best=13000), NOW)
    _emit_profile(engine_conn, _profile(best=13142), LATER)
    peaks = [e for e in _events(engine_conn) if e[0] == "best_trophies_peak"]
    assert peaks == []


def test_arena_changed_emitted_with_prev(engine_conn):
    _emit_profile(engine_conn, _profile(arena=(54000012, "Spooky Town")), NOW)
    _emit_profile(engine_conn, _profile(arena=(54000013, "Rascal's Hideout")), LATER)
    rows = [r for r in _events(engine_conn) if r[0] == "arena_changed"]
    assert rows == [("arena_changed", f"arena_changed:{TAG}:54000013")]


def test_card_unlock_and_level_milestone(engine_conn):
    def cards_payload(cards):
        return {"tag": TAG, "cards": cards, "badges": []}

    base = [{"id": 1, "name": "Knight", "rarity": "common", "level": 13, "maxLevel": 14}]
    emit(
        engine_conn,
        "player",
        TAG,
        "cards",
        project_player_aspects(cards_payload(base))["cards"],
        NOW,
    )
    grown = base + [
        {
            "id": 2,
            "name": "Mighty Miner",
            "rarity": "champion",
            "level": 1,
            "maxLevel": 4,
        }
    ]
    grown[0] = dict(base[0], level=14)  # display level 15... depends on scale
    emit(
        engine_conn,
        "player",
        TAG,
        "cards",
        project_player_aspects(cards_payload(grown))["cards"],
        LATER,
    )
    events = _events(engine_conn)
    unlocks = [e for e in events if e[0] == "card_unlocked"]
    assert unlocks == [("card_unlocked", f"card_unlocked:{TAG}:2")]
    payload = json.loads(
        engine_conn.execute(
            "SELECT payload_json FROM player_events WHERE event_type='card_unlocked'"
        ).fetchone()[0]
    )
    assert payload["rarity"] == "champion"


def _roster(members):
    return {
        "tag": "#J2RGCRVG",
        "name": "POAP KINGS",
        "clanScore": 60000,
        "clanWarTrophies": 900,
        "memberList": [
            {"tag": t, "name": n, "role": r, "trophies": 5000, "donations": d}
            for t, n, r, d in members
        ],
    }


def _emit_roster(conn, payload, at):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, "
        "is_home) VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)",
        (at,),
    )
    return emit(conn, "clan", "#J2RGCRVG", "roster", project_clan_aspects(payload)["roster"], at)


def test_roster_join_leave_role_change_maintain_memberships(engine_conn):
    _emit_roster(engine_conn, _roster([("#A", "Al", "member", 0), ("#B", "Bo", "elder", 0)]), NOW)
    # First-sight roster is silent (the emitter never runs — no identity
    # upserts, no membership rows); production's initial roster state comes
    # from the T1/T5 transforms. Simulate that carried state:
    for tag, name in (("#A", "Al"), ("#B", "Bo")):
        engine_conn.execute(
            "INSERT OR IGNORE INTO players (player_tag, current_name, "
            "first_seen_at, last_seen_at) VALUES (?, ?, '2026-03-01', ?)",
            (tag, name, NOW),
        )
        engine_conn.execute(
            "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
            "VALUES (?, '2026-03-01', 'transform')",
            (tag,),
        )
    engine_conn.commit()
    # join #C, leave #B, promote #A
    _emit_roster(
        engine_conn,
        _roster([("#A", "Al", "elder", 0), ("#C", "Cy", "member", 0)]),
        LATER,
    )
    events = _events(engine_conn, "clan_events")
    types = [e[0] for e in events]
    assert "member_joined" in types and "member_left" in types and "role_changed" in types
    open_tags = {
        r[0]
        for r in engine_conn.execute(
            "SELECT player_tag FROM clan_memberships WHERE left_at IS NULL"
        )
    }
    assert open_tags == {"#A", "#C"}
    closed = engine_conn.execute(
        "SELECT left_at FROM clan_memberships WHERE player_tag='#B'"
    ).fetchone()
    assert closed["left_at"] is not None
    direction = json.loads(
        engine_conn.execute(
            "SELECT payload_json FROM clan_events WHERE event_type='role_changed'"
        ).fetchone()[0]
    )["direction"]
    assert direction == "promoted"


def test_historical_roster_replay_reuses_membership_interval(engine_conn):
    before = "2026-07-01T09:00:00Z"
    joined = "2026-07-01T10:00:00Z"
    left = "2026-07-02T10:00:00Z"
    empty = _roster([])
    present = _roster([("#REPLAY", "Replay", "member", 0)])

    _emit_roster(engine_conn, empty, before)
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#REPLAY', 'Replay', ?, ?)",
        (joined, left),
    )
    engine_conn.execute(
        "INSERT INTO clan_memberships "
        "(player_tag, joined_at, left_at, join_source, leave_source) "
        "VALUES ('#REPLAY', ?, ?, 'roster_diff', 'leader_verified_kick')",
        (joined, left),
    )
    engine_conn.commit()

    # Two complete historical replays derive/dedup the events but must reuse
    # the already-recorded interval and preserve its verified leave reason.
    for _ in range(2):
        engine_conn.execute("DELETE FROM state_baselines")
        _emit_roster(engine_conn, empty, before)
        _emit_roster(engine_conn, present, joined)
        _emit_roster(engine_conn, empty, left)

    memberships = engine_conn.execute(
        "SELECT joined_at, left_at, leave_source FROM clan_memberships WHERE player_tag = '#REPLAY'"
    ).fetchall()
    assert [tuple(row) for row in memberships] == [(joined, left, "leader_verified_kick")]


def test_roster_change_set_derivation_is_pure_and_deterministic():
    changes = derive_roster_change_set(
        {
            "#B": {"name": "Bo", "role": "elder"},
            "#A": {"name": "Al", "role": "member"},
        },
        {
            "#C": {"name": "Cy", "role": "member"},
            "#A": {"name": "Al", "role": "elder"},
        },
        LATER,
        role_rank={"member": 0, "elder": 1},
    )
    assert [entry.player_tag for entry in changes.joins] == ["#C"]
    assert [entry.player_tag for entry in changes.leaves] == ["#B"]
    assert [entry.player_tag for entry in changes.role_transitions] == ["#A"]
    assert changes.role_transitions[0].direction == "promoted"


def test_roster_invariant_failure_keeps_baseline_retryable(engine_conn, monkeypatch):
    initial = _roster([("#A", "Al", "member", 0)])
    changed = _roster([("#B", "Bo", "member", 0)])
    _emit_roster(engine_conn, initial, NOW)
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#A', 'Al', '2026-03-01', ?)",
        (NOW,),
    )
    engine_conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES ('#A', '2026-03-01', 'transform')"
    )
    engine_conn.commit()

    from engine.emitters import clan

    monkeypatch.setattr(clan, "_emit", lambda *args, **kwargs: 0)
    with pytest.raises(ChangeSetInvariantError, match="roster change set invariant"):
        _emit_roster(engine_conn, changed, LATER)
    engine_conn.rollback()

    baseline = engine_conn.execute(
        "SELECT payload_json, observed_at FROM state_baselines "
        "WHERE entity_kind='clan' AND entity_tag='#J2RGCRVG' AND aspect='roster'"
    ).fetchone()
    assert baseline["observed_at"] == NOW
    assert set(json.loads(baseline["payload_json"])["members"]) == {"#A"}
    open_tags = {
        row[0]
        for row in engine_conn.execute(
            "SELECT player_tag FROM clan_memberships WHERE left_at IS NULL"
        )
    }
    assert open_tags == {"#A"}


def test_weekly_donation_leader_reset_top3(engine_conn):
    _emit_roster(
        engine_conn,
        _roster(
            [
                ("#A", "Al", "member", 300),
                ("#B", "Bo", "member", 200),
                ("#C", "Cy", "member", 100),
                ("#D", "Dy", "member", 50),
            ]
        ),
        NOW,
    )
    # Monday reset: everyone collapses to ~0
    _emit_roster(
        engine_conn,
        _roster(
            [
                ("#A", "Al", "member", 0),
                ("#B", "Bo", "member", 0),
                ("#C", "Cy", "member", 0),
                ("#D", "Dy", "member", 0),
            ]
        ),
        LATER,
    )
    row = engine_conn.execute(
        "SELECT payload_json FROM clan_events WHERE event_type='weekly_donation_leader'"
    ).fetchone()
    assert row is not None, "reset should emit the donation leader"
    payload = json.loads(row[0])
    leaders = payload["leaders"]
    assert [x["tag"] for x in leaders] == ["#A", "#B", "#C"]  # top-3, ordered
    assert leaders[0]["donations"] == 300


def test_calendar_birthday_and_anniversary(engine_conn):
    engine_conn.execute(
        "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)",
        (NOW,),
    )
    engine_conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES ('#A', 'Al', ?, ?)",
        (NOW, NOW),
    )
    engine_conn.execute(
        "INSERT INTO player_metadata (player_tag, birth_month, birth_day) VALUES ('#A', 7, 1)"
    )
    engine_conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source) "
        "VALUES ('#A', '2026-04-01', 'test')"
    )
    engine_conn.commit()
    n = emit_calendar(engine_conn, "2026-07-01")
    events = _events(engine_conn, "clan_events")
    types = [e[0] for e in events]
    assert "member_birthday" in types
    # joined 04-01 → 07-01 is 3 months → join_anniversary (months %3 == 0)
    assert "join_anniversary" in types
    assert n == len(events)
    # idempotent: same day re-run adds nothing
    assert emit_calendar(engine_conn, "2026-07-01") == 0


def _home_clan(conn):
    conn.execute(
        "INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', '2026-02-04', ?, 1)",
        (NOW,),
    )


def _cake_member(
    conn,
    tag,
    name,
    *,
    birth=(None, None),
    joined="2026-06-15",
    cr_years=None,
    cr_celebrated=None,
    left_at=None,
):
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (tag, name, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO player_metadata (player_tag, birth_month, birth_day, "
        "cr_account_age_years, cr_years_celebrated) VALUES (?, ?, ?, ?, ?)",
        (tag, birth[0], birth[1], cr_years, cr_celebrated),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, left_at, join_source) "
        "VALUES (?, ?, ?, 'test')",
        (tag, joined, left_at),
    )


def _cr_years_celebrated(conn, tag):
    return conn.execute(
        "SELECT cr_years_celebrated FROM player_metadata WHERE player_tag = ?", (tag,)
    ).fetchone()[0]


def test_cr_account_anniversary_ticks_up(engine_conn):
    _home_clan(engine_conn)
    _cake_member(engine_conn, "#A", "Al", cr_years=5)  # cr_years_celebrated NULL
    engine_conn.commit()

    # First sight: baseline only — never celebrate a member's current age.
    emit_calendar(engine_conn, "2026-07-01")
    assert "cr_account_anniversary" not in [e[0] for e in _events(engine_conn, "clan_events")]
    assert _cr_years_celebrated(engine_conn, "#A") == 5

    # A year tick-up emits exactly one anniversary and advances the baseline.
    engine_conn.execute(
        "UPDATE player_metadata SET cr_account_age_years = 6 WHERE player_tag = '#A'"
    )
    emit_calendar(engine_conn, "2026-07-01")
    cr_events = [e for e in _events(engine_conn, "clan_events") if e[0] == "cr_account_anniversary"]
    assert cr_events == [("cr_account_anniversary", "cr_account_anniversary:#A:6")]
    payload = json.loads(
        engine_conn.execute(
            "SELECT payload_json FROM clan_events WHERE event_type='cr_account_anniversary'"
        ).fetchone()[0]
    )
    assert payload["years"] == 6 and payload["name"] == "Al"
    assert _cr_years_celebrated(engine_conn, "#A") == 6

    # Idempotent: nothing left to celebrate.
    assert emit_calendar(engine_conn, "2026-07-01") == 0


def test_cr_account_anniversary_skips_departed_member(engine_conn):
    _home_clan(engine_conn)
    # A departed member whose years already rose — must never be celebrated.
    _cake_member(engine_conn, "#B", "Bo", cr_years=6, cr_celebrated=5, left_at="2026-06-20")
    engine_conn.commit()
    emit_calendar(engine_conn, "2026-07-01")
    assert "cr_account_anniversary" not in [e[0] for e in _events(engine_conn, "clan_events")]


def test_join_anniversary_flags_annual_marks(engine_conn):
    _home_clan(engine_conn)
    _cake_member(engine_conn, "#A", "Al", joined="2026-07-01")  # 12 months → annual
    _cake_member(engine_conn, "#C", "Cy", joined="2027-04-01")  # 3 months → not annual
    engine_conn.commit()
    emit_calendar(engine_conn, "2027-07-01")

    payloads = {
        r["subject_tag"]: json.loads(r["payload_json"])
        for r in engine_conn.execute(
            "SELECT subject_tag, payload_json FROM clan_events WHERE event_type='join_anniversary'"
        )
    }
    assert payloads["#A"]["is_annual"] is True
    assert payloads["#A"]["months"] == 12 and payloads["#A"]["years"] == 1
    assert payloads["#C"]["is_annual"] is False
    assert payloads["#C"]["months"] == 3


def test_badge_earned_payload_carries_the_resolved_card_and_id(engine_conn):
    """Resolve once, at the source. Every reader that decoded the raw key itself
    got a vote on whether to do it right, and the awareness brain voted wrong —
    it posted "Dark Witch" (there is no such card; it is Night Witch) to the clan
    on 2026-07-03. The emitter now stamps the label, the card, and the catalog's
    own card_id into the payload, so no later reader has to know the mapping."""
    engine_conn.execute(
        "INSERT INTO card_catalog (card_id, name, rarity, card_type, synced_at) VALUES (?,?,?,?,?)",
        (26000048, "Night Witch", "legendary", "troop", NOW),
    )
    _emit_profile(engine_conn, _profile(badges={}), NOW)
    _emit_profile(engine_conn, _profile(badges={"MasteryDarkWitch": 3}), LATER)

    payload = json.loads(
        engine_conn.execute(
            "SELECT payload_json FROM player_events WHERE event_type='badge_earned'"
        ).fetchone()[0]
    )
    assert payload["badge_label"] == "Card Mastery: Night Witch"
    assert payload["card_name"] == "Night Witch"
    assert payload["card_id"] == 26000048
    # The raw key stays — it is what the API said, and the dedup key is built on
    # it — but it is now identity only, never the words anyone reads.
    assert payload["badge_name"] == "MasteryDarkWitch"


def test_badge_earned_never_invents_a_card_absent_from_the_catalog(engine_conn):
    """A card released after the key map was written must yield no card at all,
    not a plausible camelCase guess. Fail closed: no card_name, so no card_id."""
    _emit_profile(engine_conn, _profile(badges={}), NOW)
    _emit_profile(engine_conn, _profile(badges={"MasteryNotARealCard": 1}), LATER)

    payload = json.loads(
        engine_conn.execute(
            "SELECT payload_json FROM player_events WHERE event_type='badge_earned'"
        ).fetchone()[0]
    )
    assert payload["badge_label"] == "a new Card Mastery badge"
    assert "card_name" not in payload and "card_id" not in payload
