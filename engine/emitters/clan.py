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

from datetime import date, datetime, timedelta

from engine.db import canon_tag, utcnow
from engine.emitters import insert_stream_event

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
            "exp_level": m.get("expLevel"),
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


def _upsert_identity(conn, tag: str, name: str | None, observed_at: str) -> None:
    conn.execute(
        """INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(player_tag) DO UPDATE SET
               current_name = COALESCE(excluded.current_name, players.current_name),
               last_seen_at = excluded.last_seen_at""",
        (tag, name, observed_at, observed_at),
    )
    if name:
        conn.execute(
            "INSERT OR IGNORE INTO player_aliases (player_tag, alias, source, observed_at) "
            "VALUES (?, ?, 'roster', ?)",
            (tag, name, observed_at),
        )


def emit_roster(conn, clan_tag, old, new, observed_at, window_start) -> int:
    n = 0
    old_members = old.get("members") or {}
    new_members = new.get("members") or {}

    for tag, info in new_members.items():
        _upsert_identity(conn, tag, (info or {}).get("name"), observed_at)

    # member_joined — also opens the membership row (events.md §4)
    for tag, info in new_members.items():
        if tag in old_members:
            continue
        info = info or {}
        n += _emit(conn, clan_tag, tag, observed_at, window_start, "member_joined",
                   f"member_joined:{tag}:{observed_at}",
                   {"name": info.get("name"), "trophies": info.get("trophies"),
                    "role": info.get("role"), "exp_level": info.get("exp_level")})
        open_row = conn.execute(
            "SELECT 1 FROM clan_memberships WHERE player_tag = ? AND left_at IS NULL",
            (tag,),
        ).fetchone()
        if open_row is None:
            conn.execute(
                "INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, join_source) "
                "VALUES (?, ?, ?, 'roster_diff')",
                (tag, clan_tag, observed_at),
            )

    # member_left — closes the membership row; kick-suppression is
    # RECOGNITION's job (C1) — the event always exists.
    for tag, info in old_members.items():
        if tag in new_members:
            continue
        info = info or {}
        tenure = conn.execute(
            "SELECT CAST(julianday(?) - julianday(joined_at) AS INTEGER) "
            "FROM clan_memberships WHERE player_tag = ? AND left_at IS NULL",
            (observed_at, tag),
        ).fetchone()
        n += _emit(conn, clan_tag, tag, observed_at, window_start, "member_left",
                   f"member_left:{tag}:{observed_at}",
                   {"name": info.get("name"), "role": info.get("role"),
                    "trophies": info.get("trophies"),
                    "tenure_days": tenure[0] if tenure and tenure[0] is not None else None})
        conn.execute(
            "UPDATE clan_memberships SET left_at = ?, leave_source = 'roster_diff' "
            "WHERE player_tag = ? AND left_at IS NULL",
            (observed_at, tag),
        )

    # role_changed — promotions AND demotions are first-class events; the
    # public-post decision is recognition's (demotions never post, §4).
    for tag, info in new_members.items():
        prev = old_members.get(tag)
        if not prev:
            continue
        old_role, new_role = (prev.get("role") or ""), ((info or {}).get("role") or "")
        if old_role and new_role and old_role != new_role:
            direction = (
                "promoted"
                if _ROLE_RANK.get(new_role, -1) > _ROLE_RANK.get(old_role, -1)
                else "demoted"
            )
            n += _emit(conn, clan_tag, tag, observed_at, window_start, "role_changed",
                       f"role_changed:{tag}:{new_role}:{observed_at}",
                       {"new_role": new_role, "prev_role": old_role,
                        "direction": direction})

    n += _emit_donation_reset(conn, clan_tag, old_members, new_members, observed_at, window_start)
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


def emit_calendar(conn, today_chicago: str) -> int:
    """Clock-driven calendar events (events.md §4) — runs on the first tick of
    each America/Chicago day. Port of CakeDayDetector (detectors.py:920–995);
    date-embedded dedup keys are the idempotency."""
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
            n += cal_event("join_anniversary", f"join_anniversary:{r['tag']}:{today_chicago}",
                           r["tag"], {"name": r["name"], "years": months // 12,
                                      "months": months})
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
