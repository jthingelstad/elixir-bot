"""The engine-reconciliation backstop: an OPEN member-review case the management
engine no longer corroborates (state=none, past the grace window, no open card)
is dismissed. Motivated by Ratko #365 — a promotion_review with promote_state=none
and no backing card that nagged the awareness read as "due" forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from storage import cases

NOW = "2026-07-10T12:00:00Z"
NOW_DT = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _opened(days_ago: int) -> str:
    return (NOW_DT - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _member(
    conn,
    tag="#RAT",
    name="Ratko",
    *,
    promote="none",
    kick="none",
    demote="none",
    in_clan=True,
):
    conn.execute(
        "INSERT OR IGNORE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES ('#J2RGCRVG', 'POAP KINGS', ?, ?, 1)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (tag, name, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO clan_memberships (player_tag, joined_at, join_source, left_at) "
        "VALUES (?, ?, 'test', ?)",
        (tag, NOW, None if in_clan else NOW),
    )
    conn.execute(
        "INSERT INTO member_management (player_tag, computed_at, week_anchor, "
        " kick_state, promote_state, demote_state) VALUES (?, ?, '2026-07-06', ?, ?, ?)",
        (tag, NOW, kick, promote, demote),
    )
    conn.commit()


def _case(
    conn,
    tag="#RAT",
    name="Ratko",
    *,
    case_type="promotion_review",
    status="open",
    opened_at=None,
):
    conn.execute(
        "INSERT INTO decision_cases (case_key, case_type, status, title, target_player_tag, "
        " target_player_name, opened_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"{case_type}:{tag}",
            case_type,
            status,
            f"{case_type}: {name}",
            tag,
            name,
            opened_at or _opened(9),
            NOW,
            NOW,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT case_id FROM decision_cases WHERE case_key=?", (f"{case_type}:{tag}",)
    ).fetchone()["case_id"]


def _proposed_card(conn, tag="#RAT", action_type="promotion_recommendation"):
    conn.execute(
        "INSERT INTO leader_action_recommendations "
        "(action_key, action_type, objective, status, prompt_text, proposed_at, "
        " created_at, updated_at, target_player_tag, is_test) "
        "VALUES (?, ?, 'o', 'proposed', 'p', ?, ?, ?, ?, 0)",
        (f"c:{tag}:{action_type}", action_type, NOW, NOW, NOW, tag),
    )
    conn.commit()


def _status(conn, case_id):
    return conn.execute("SELECT status FROM decision_cases WHERE case_id=?", (case_id,)).fetchone()[
        "status"
    ]


def test_dismisses_stale_uncorroborated_promotion_case(engine_conn):
    # The Ratko #365 shape: open promotion_review, promote_state=none, 9 days old,
    # no backing card → dismissed.
    _member(engine_conn, promote="none")
    cid = _case(engine_conn, case_type="promotion_review", opened_at=_opened(9))
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert [c["case_id"] for c in out] == [cid]
    assert _status(engine_conn, cid) == "dismissed"


def test_keeps_case_inside_grace_window(engine_conn):
    # Same shape but only 3 days old — inside the 7d grace, give the flag time.
    _member(engine_conn, promote="none")
    cid = _case(engine_conn, case_type="promotion_review", opened_at=_opened(3))
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert out == []
    assert _status(engine_conn, cid) == "open"


def test_keeps_case_the_engine_still_tracks(engine_conn):
    # promote_state='building' → the engine IS tracking this member; not stale.
    _member(engine_conn, promote="building")
    cid = _case(engine_conn, case_type="promotion_review", opened_at=_opened(30))
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert out == []
    assert _status(engine_conn, cid) == "open"


def test_keeps_case_with_open_card(engine_conn):
    # state=none but an open proposed card exists → a human still has it in flight.
    _member(engine_conn, promote="none")
    cid = _case(engine_conn, case_type="promotion_review", opened_at=_opened(30))
    _proposed_card(engine_conn, action_type="promotion_recommendation")
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert out == []
    assert _status(engine_conn, cid) == "open"


def test_inactivity_case_uses_kick_state(engine_conn):
    # Dimension mapping: inactivity_review reads kick_state.
    _member(engine_conn, tag="#IDL", name="Idle", kick="none")
    cid = _case(
        engine_conn,
        tag="#IDL",
        name="Idle",
        case_type="inactivity_review",
        opened_at=_opened(9),
    )
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert [c["case_id"] for c in out] == [cid]
    assert _status(engine_conn, cid) == "dismissed"


def test_kick_watch_state_is_not_dismissed(engine_conn):
    # kick_state='watch' (trending) → engine is tracking; keep the review.
    _member(engine_conn, tag="#WCH", name="Watched", kick="watch")
    cid = _case(
        engine_conn,
        tag="#WCH",
        name="Watched",
        case_type="inactivity_review",
        opened_at=_opened(30),
    )
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert out == []
    assert _status(engine_conn, cid) == "open"


def test_departed_member_left_to_departed_reconciler(engine_conn):
    # A departed member's case is NOT touched here (the departed reconciler owns
    # the kick-vs-leave distinction); this backstop only reaps in-clan members.
    _member(engine_conn, tag="#GON", name="Gone", promote="none", in_clan=False)
    cid = _case(
        engine_conn,
        tag="#GON",
        name="Gone",
        case_type="promotion_review",
        opened_at=_opened(30),
    )
    out = cases.reconcile_uncorroborated_member_cases(now=NOW, conn=engine_conn)
    assert out == []
    assert _status(engine_conn, cid) == "open"
