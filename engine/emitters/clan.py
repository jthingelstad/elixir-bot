"""Clan-stream emitters — events.md §4 (aspects roster / clan_entity + calendar).

Roster diffs own membership reality: member_joined opens a clan_memberships
row, member_left closes it, role_changed carries direction (demotions become
first-class instead of invisible — §14.5). The roster emitter also maintains
the identity layer (players.current_name / last_seen_at, player_aliases).

weekly_donation_leader fires at the observed weekly reset, computed from the
PREVIOUS baseline inside the same transaction (events.md §4 owner note),
payload top-3 (TOP_N = 3 carried from WeeklyDonationLeaderDetector).

The calendar emitter is clock-driven (first tick of each Chicago day,
runtime.md §2 step 3) — port of CakeDayDetector (detectors.py:920–995).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from engine.change_sets import (
    ChangeSetInvariantError,
    RosterChangeSet,
    derive_roster_change_set,
)
from engine.db import canon_tag, utcnow
from engine.emitters import insert_stream_event

_log = logging.getLogger("engine.emitters.clan")
HOME_CLAN = "#J2RGCRVG"
_ROLE_RANK = {"member": 0, "elder": 1, "coleader": 2, "leader": 3}  # detectors.py:611
DONATION_LEADER_TOP_N = 3          # detectors.py:1000 TOP_N
DONATION_RESET_RATIO = 0.1         # reset = totals collapse below 10% of baseline
DONATION_RESET_MIN_PREV = 100      # ...and the baseline week was a real week
CLAN_SCORE_MILESTONE_STEP = 10_000  # NEW constant (no Gen C ancestor; events.md §4)
# War-league tiers from clanWarTrophies — game knowledge, not payload fields.
WAR_LEAGUE_TIERS = (
    (5000, "Legendary League III"), (4000, "Legendary League II"),
    (3000, "Legendary League I"), (2500, "Gold League III"),
    (2000, "Gold League II"), (1500, "Gold League I"),
    (1200, "Silver League III"), (900, "Silver League II"),
    (600, "Silver League I"), (400, "Bronze League III"),
    (200, "Bronze League II"), (0, "Bronze League I"),
)


def project_clan_aspects(payload: dict) -> dict[str, dict]:
    """Split a clan API payload into roster + clan_entity baselines."""
    members = {}
    for m in payload.get("memberList") or []:
        tag = canon_tag(m.get("tag"))
        if not tag:
            continue
        members[tag] = {
            "name": m.get("name"),
            "role": (m.get("role") or "").lower(),
            "trophies": m.get("trophies"),
            "donations": m.get("donations"),
            "donations_received": m.get("donationsReceived"),
            # memberList.expLevel is 0 for every member (dead at the CR API;
            # normalize.md catalog). None keeps profile-sourced truth intact.
            "exp_level": m.get("expLevel") or None,
            "clan_rank": m.get("clanRank"),
            "previous_clan_rank": m.get("previousClanRank"),
        }
    roster = {"members": members}
    clan_entity = {
        "name": payload.get("name"),
        "clan_score": payload.get("clanScore"),
        "war_trophies": payload.get("clanWarTrophies"),
    }
    return {"roster": roster, "clan_entity": clan_entity}


def _emit(conn, clan_tag, subject_tag, observed_at, window_start, event_type, dedup_key, payload) -> int:
    return insert_stream_event(
        conn,
        "clan_events",
        dedup_key=dedup_key,
        event_type=event_type,
        subject_cols={"clan_tag": clan_tag, "subject_tag": subject_tag},
        observed_at=observed_at,
        window_start=window_start,
        payload=payload,
    )


def _display_name(conn, tag: str, raw_name: str | None) -> str | None:
    """The preferred readable name for event payloads (so render_intent's
    deterministic fallback copy uses the nickname/cleaned form, not the raw)."""
    from storage._formatting import preferred_display_name

    return preferred_display_name(conn, tag, raw_name) or raw_name


def _upsert_identity(conn, tag: str, name: str | None, observed_at: str) -> None:
    # Upsert current_name + the materialized, injection-safe display_name in one
    # place (normalize-at-source) via the shared engine helper.
    from engine.db import ensure_player, refresh_display_name

    ensure_player(conn, tag, name, observed_at)
    if name:
        conn.execute(
            "INSERT OR IGNORE INTO player_aliases (player_tag, alias, source, observed_at) "
            "VALUES (?, ?, 'roster', ?)",
            (tag, name, observed_at),
        )
    # First-sight / rename hook: guarantee a readable nickname exists for a
    # residual name (e.g. "...") before any reference is composed downstream,
    # and drop a stale generated one on a rename-to-readable. Fail-soft — a
    # nickname hiccup must never break roster ingest.
    try:
        from engine.nicknames import ensure_nickname

        ensure_nickname(conn, tag, name, observed_at)
        # ensure_nickname may have set OR cleared the nickname (rename-to-readable);
        # either changes the tier-1 input, so re-materialize display_name from the
        # final state. Cheap, and always correct regardless of what changed.
        refresh_display_name(conn, tag, name)
    except Exception:
        _log.exception("ensure_nickname failed for %s (%r)", tag, name)


def _apply_roster_change_set(
    conn, clan_tag: str, changes: RosterChangeSet, window_start: str | None,
) -> int:
    """Apply one already-derived roster transition inside the caller's transaction."""
    n = 0
    for join in changes.joins:
        info = join.info
        n += _emit(
            conn, clan_tag, join.player_tag, changes.observed_at, window_start,
            "member_joined", join.dedup_key,
            {"name": _display_name(conn, join.player_tag, info.get("name")),
             "trophies": info.get("trophies"),
             "role": info.get("role"), "exp_level": info.get("exp_level")},
        )
        open_row = conn.execute(
            "SELECT 1 FROM clan_memberships "
            "WHERE player_tag = ? AND clan_tag = ? AND left_at IS NULL",
            (join.player_tag, clan_tag),
        ).fetchone()
        if open_row is None:
            conn.execute(
                "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
                "VALUES (?, ?, ?, 'roster_diff')",
                (join.player_tag, clan_tag, changes.observed_at),
            )

    for leave in changes.leaves:
        info = leave.info
        tenure = conn.execute(
            "SELECT CAST(julianday(?) - julianday(joined_at) AS INTEGER) "
            "FROM clan_memberships "
            "WHERE player_tag = ? AND clan_tag = ? AND left_at IS NULL",
            (changes.observed_at, leave.player_tag, clan_tag),
        ).fetchone()
        n += _emit(
            conn, clan_tag, leave.player_tag, changes.observed_at, window_start,
            "member_left", leave.dedup_key,
            {"name": _display_name(conn, leave.player_tag, info.get("name")),
             "role": info.get("role"),
             "trophies": info.get("trophies"),
             "tenure_days": tenure[0] if tenure and tenure[0] is not None else None},
        )
        conn.execute(
            "UPDATE clan_memberships SET left_at = ?, leave_source = 'roster_diff' "
            "WHERE player_tag = ? AND clan_tag = ? AND left_at IS NULL",
            (changes.observed_at, leave.player_tag, clan_tag),
        )

    for role in changes.role_transitions:
        n += _emit(
            conn, clan_tag, role.player_tag, changes.observed_at, window_start,
            "role_changed", role.dedup_key,
            {"new_role": role.new_role, "prev_role": role.previous_role,
             "direction": role.direction},
        )
    return n


def _verify_roster_change_set(conn, clan_tag: str, changes: RosterChangeSet) -> None:
    """Refuse baseline advancement unless events and membership truth agree."""
    failures: list[str] = []
    for join in changes.joins:
        event = conn.execute(
            "SELECT 1 FROM clan_events WHERE dedup_key = ? AND event_type = 'member_joined'",
            (join.dedup_key,),
        ).fetchone()
        memberships = conn.execute(
            "SELECT COUNT(*) FROM clan_memberships "
            "WHERE player_tag = ? AND clan_tag = ? AND left_at IS NULL",
            (join.player_tag, clan_tag),
        ).fetchone()[0]
        if event is None or memberships != 1:
            failures.append(
                f"join {join.player_tag} event={event is not None} open={memberships}"
            )
    for leave in changes.leaves:
        event = conn.execute(
            "SELECT 1 FROM clan_events WHERE dedup_key = ? AND event_type = 'member_left'",
            (leave.dedup_key,),
        ).fetchone()
        memberships = conn.execute(
            "SELECT COUNT(*) FROM clan_memberships "
            "WHERE player_tag = ? AND clan_tag = ? AND left_at IS NULL",
            (leave.player_tag, clan_tag),
        ).fetchone()[0]
        if event is None or memberships:
            failures.append(
                f"leave {leave.player_tag} event={event is not None} open={memberships}"
            )
    for role in changes.role_transitions:
        event = conn.execute(
            "SELECT 1 FROM clan_events WHERE dedup_key = ? AND event_type = 'role_changed'",
            (role.dedup_key,),
        ).fetchone()
        if event is None:
            failures.append(f"role {role.player_tag} event=False")
    if failures:
        raise ChangeSetInvariantError(
            "roster change set invariant failed: " + "; ".join(failures)
        )


def emit_roster(conn, clan_tag, old, new, observed_at, window_start) -> int:
    old_members = old.get("members") or {}
    new_members = new.get("members") or {}
    changes = derive_roster_change_set(
        old_members, new_members, observed_at, role_rank=_ROLE_RANK,
    )

    for tag in sorted(new_members):
        info = new_members[tag] or {}
        _upsert_identity(conn, tag, info.get("name"), observed_at)

    n = _apply_roster_change_set(conn, clan_tag, changes, window_start)

    n += _emit_donation_reset(conn, clan_tag, old_members, new_members, observed_at, window_start)
    _verify_roster_change_set(conn, clan_tag, changes)
    return n


# A verified LEAVE only earns a public goodbye within this window of the
# departure — a stale confirmation days later doesn't warrant a sendoff.
_VERIFIED_LEAVE_WINDOW_DAYS = 5


def emit_verified_leave_events(conn, clan_tag, observed_at) -> int:
    """Emit ``member_left_verified`` for departures a leader has confirmed were an
    organic LEAVE (``clan_memberships.leave_source = 'leader_verified_leave'``).

    The raw ``member_left`` is NOT a public-post trigger anymore — a farewell is
    held until a leader verifies, so a member kicked for behavior never gets a
    warm goodbye. This is the signal the awareness brain narrates. A confirmed
    KICK emits nothing. Idempotent via the dedup key; bounded to recent leaves."""
    base = datetime.fromisoformat(str(observed_at).replace("Z", "").replace("z", ""))
    cutoff = (base - timedelta(days=_VERIFIED_LEAVE_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT cm.player_tag, cm.left_at,
                  COALESCE(p.display_name, p.current_name, cm.player_tag) AS name,
                  CAST(julianday(cm.left_at) - julianday(cm.joined_at) AS INTEGER) AS tenure_days
           FROM clan_memberships cm
           LEFT JOIN players p ON p.player_tag = cm.player_tag
           WHERE cm.leave_source = 'leader_verified_leave' AND cm.left_at >= ?""",
        (cutoff,),
    ).fetchall()
    n = 0
    for row in rows:
        n += _emit(conn, clan_tag, row["player_tag"], observed_at, None,
                   "member_left_verified",
                   f"member_left_verified:{row['player_tag']}:{row['left_at']}",
                   {"name": row["name"], "tenure_days": row["tenure_days"]})
    return n


def _emit_donation_reset(conn, clan_tag, old_members, new_members, observed_at, window_start) -> int:
    """weekly_donation_leader — the roster emitter detects the CR Monday reset
    (donations collapse toward zero vs baseline) and computes the just-closed
    week's top donors from the PREVIOUS baseline (events.md §4)."""
    prev_total = sum((m or {}).get("donations") or 0 for m in old_members.values())
    new_total = sum((m or {}).get("donations") or 0 for m in new_members.values())
    if prev_total < DONATION_RESET_MIN_PREV or new_total >= prev_total * DONATION_RESET_RATIO:
        return 0
    leaders = sorted(
        (
            {"tag": tag, "name": (m or {}).get("name"), "donations": (m or {}).get("donations") or 0}
            for tag, m in old_members.items()
            if ((m or {}).get("donations") or 0) > 0
        ),
        key=lambda x: -x["donations"],
    )[:DONATION_LEADER_TOP_N]
    if not leaders:
        return 0
    # The just-closed week: the day before the reset observation, ISO-keyed.
    obs_date = date.fromisoformat(str(observed_at)[:10]) - timedelta(days=1)
    iso = obs_date.isocalendar()
    week_key = f"{iso[0]}W{iso[1]:02d}"
    return _emit(conn, clan_tag, leaders[0]["tag"], observed_at, window_start,
                 "weekly_donation_leader", f"weekly_donation_leader:{week_key}",
                 {"week_ending": obs_date.isoformat(), "leaders": leaders})


def _war_league(war_trophies) -> str | None:
    if not isinstance(war_trophies, int):
        return None
    for floor, name in WAR_LEAGUE_TIERS:
        if war_trophies >= floor:
            return name
    return None


def emit_clan_entity(conn, clan_tag, old, new, observed_at, window_start) -> int:
    n = 0
    # clan_score_milestone — every CLAN_SCORE_MILESTONE_STEP (new type, §9.3)
    old_score, new_score = old.get("clan_score"), new.get("clan_score")
    if isinstance(old_score, int) and isinstance(new_score, int) and new_score > old_score > 0:
        first = (old_score // CLAN_SCORE_MILESTONE_STEP + 1) * CLAN_SCORE_MILESTONE_STEP
        for milestone in range(first, new_score + 1, CLAN_SCORE_MILESTONE_STEP):
            n += _emit(conn, clan_tag, None, observed_at, window_start,
                       "clan_score_milestone", f"clan_score_milestone:{clan_tag}:{milestone}",
                       {"milestone": milestone, "clan_score": new_score})
    # clan_league_changed — war-league tier movement (new type, §9.3)
    old_league = _war_league(old.get("war_trophies"))
    new_league = _war_league(new.get("war_trophies"))
    if old_league and new_league and old_league != new_league:
        n += _emit(conn, clan_tag, None, observed_at, window_start,
                   "clan_league_changed", f"clan_league_changed:{clan_tag}:{new_league}",
                   {"league": new_league, "prev_league": old_league,
                    "war_trophies": new.get("war_trophies")})
    conn.execute(
        """INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(clan_tag) DO UPDATE SET
               name = COALESCE(excluded.name, clans.name),
               last_seen_at = excluded.last_seen_at""",
        (clan_tag, new.get("name"), observed_at, observed_at, int(clan_tag == HOME_CLAN)),
    )
    return n


def _clan_founded(conn) -> str | None:
    """Founding date from prompts config (CakeDayDetector parity); the clans
    row has no founded column."""
    try:
        import prompts

        return prompts.thresholds().get("clan_founded")
    except Exception:
        return None


def _ensure_cr_years_column(conn) -> None:
    """Validate the centrally migrated CR-anniversary baseline column."""
    from db.schema import require_columns

    require_columns(conn, "player_metadata", {"cr_years_celebrated"})


def emit_calendar(conn, today_chicago: str) -> int:
    """Clock-driven calendar events (events.md §4) — runs on the first tick of
    each America/Chicago day. Port of CakeDayDetector (detectors.py:920–995);
    date-embedded dedup keys are the idempotency. Every cake day is gated to
    ACTIVE members (clan_memberships.left_at IS NULL) — a departed ex-member is
    never celebrated."""
    _ensure_cr_years_column(conn)
    n = 0
    stamp = f"{today_chicago}T12:00:00Z"
    today = date.fromisoformat(today_chicago)

    def cal_event(event_type, dedup_key, subject_tag, payload):
        return insert_stream_event(
            conn, "clan_events",
            dedup_key=dedup_key, event_type=event_type,
            subject_cols={"clan_tag": HOME_CLAN, "subject_tag": subject_tag},
            observed_at=stamp, window_start=None,
            payload=payload, evidence={"source": "calendar"},
        )

    founded = _clan_founded(conn)
    if founded and founded[5:] == today_chicago[5:]:
        years = today.year - int(founded[:4])
        if years >= 1:
            n += cal_event("clan_birthday", f"clan_birthday:{today_chicago}", None,
                           {"years": years})

    for r in conn.execute(
        """SELECT p.player_tag AS tag, p.current_name AS name
           FROM player_metadata md
           JOIN players p ON p.player_tag = md.player_tag
           JOIN clan_memberships cm ON cm.player_tag = p.player_tag AND cm.left_at IS NULL
           WHERE md.birth_month = ? AND md.birth_day = ?""",
        (today.month, today.day),
    ).fetchall():
        n += cal_event("member_birthday", f"member_birthday:{r['tag']}:{today_chicago}",
                       r["tag"], {"name": r["name"]})

    for r in conn.execute(
        """SELECT p.player_tag AS tag, p.current_name AS name, cm.joined_at AS joined_at
           FROM clan_memberships cm
           JOIN players p ON p.player_tag = cm.player_tag
           WHERE cm.left_at IS NULL AND cm.joined_at IS NOT NULL""",
    ).fetchall():
        try:
            jd = datetime.fromisoformat(str(r["joined_at"])[:10]).date()
        except ValueError:
            continue
        if jd.day != today.day:
            continue
        months = (today.year - jd.year) * 12 + (today.month - jd.month)
        if months >= 3 and months % 3 == 0:  # quarterly milestones (detectors.py:984)
            # Annual marks (12/24 mo) are the real anniversary — flag them so the
            # brain makes a bigger deal of them than a routine 3/6/9-month touch.
            n += cal_event("join_anniversary", f"join_anniversary:{r['tag']}:{today_chicago}",
                           r["tag"], {"name": r["name"], "years": months // 12,
                                      "months": months, "is_annual": months % 12 == 0})

    # CR account anniversary — no true creation date exists (the CR API only
    # exposes a gameplay-derived "years played" badge), so we celebrate the
    # tick-up: when a member's cr_account_age_years rises above the value we last
    # celebrated. First sight sets a silent baseline (no post). Active only.
    for r in conn.execute(
        """SELECT p.player_tag AS tag, p.current_name AS name,
                  md.cr_account_age_years AS years, md.cr_years_celebrated AS celebrated
           FROM player_metadata md
           JOIN players p ON p.player_tag = md.player_tag
           JOIN clan_memberships cm ON cm.player_tag = p.player_tag AND cm.left_at IS NULL
           WHERE md.cr_account_age_years IS NOT NULL""",
    ).fetchall():
        years = r["years"]
        celebrated = r["celebrated"]
        if celebrated is None:
            # Baseline only — never celebrate a member's current age on first sight.
            conn.execute(
                "UPDATE player_metadata SET cr_years_celebrated = ? WHERE player_tag = ?",
                (years, r["tag"]),
            )
            continue
        if years > celebrated:
            n += cal_event("cr_account_anniversary",
                           f"cr_account_anniversary:{r['tag']}:{years}",
                           r["tag"], {"name": r["name"], "years": years})
            conn.execute(
                "UPDATE player_metadata SET cr_years_celebrated = ? WHERE player_tag = ?",
                (years, r["tag"]),
            )
    return n


def calendar_already_ran(conn, today_chicago: str) -> bool:
    """Cheap first-tick-of-day check via a stream cursor row."""
    row = conn.execute(
        "SELECT cursor_text FROM stream_cursors WHERE consumer_key = 'emit:calendar' AND scope_key = ''"
    ).fetchone()
    return bool(row and row[0] == today_chicago)


def mark_calendar_ran(conn, today_chicago: str) -> None:
    conn.execute(
        """INSERT INTO stream_cursors (consumer_key, scope_key, cursor_text, updated_at)
           VALUES ('emit:calendar', '', ?, ?)
           ON CONFLICT(consumer_key, scope_key)
           DO UPDATE SET cursor_text = excluded.cursor_text, updated_at = excluded.updated_at""",
        (today_chicago, utcnow()),
    )
