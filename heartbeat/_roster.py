"""heartbeat._roster — Non-war member detectors."""

import json
import logging
from datetime import datetime, timedelta

import cr_knowledge
import db
import prompts
from heartbeat._helpers import _enrich_leave_signal

log = logging.getLogger("elixir_heartbeat")

# A deck swap of at least this many cards is "meaningful." Smaller tweaks
# (swap the log for zap) show up constantly and aren't worth surfacing; four
# or more cards changing is typically an archetype shift.
DECK_SWAP_CARD_THRESHOLD = 4


def detect_joins_leaves(current_members, known_snapshot, conn=None):
    """Compare current roster to known snapshot for joins/departures.

    current_members: list of member dicts from CR API memberList.
    known_snapshot: dict of {tag: name} from the previous roster.

    Returns (signals, updated_snapshot).
    """
    current = {m["tag"]: m["name"] for m in current_members}
    signals = []

    for tag, name in current.items():
        if tag not in known_snapshot:
            signals.append({
                "type": "member_join",
                "tag": tag,
                "name": name,
                "signal_log_type": f"member_join:{tag}",
            })

    for tag, name in known_snapshot.items():
        if tag not in current:
            signals.append(_enrich_leave_signal(tag, name, conn))

    return signals, current


def detect_arena_changes(conn=None):
    """Check DB for arena changes since last snapshot."""
    milestones = db.detect_milestones(conn=conn)
    return [
        {
            "type": "arena_change",
            "tag": m["tag"],
            "name": m["name"],
            "old_arena": m["old_value"],
            "new_arena": m["new_value"],
            "signal_log_type": m.get("signal_log_type"),
        }
        for m in milestones
        if m["type"] == "arena_change"
    ]


def detect_role_changes(conn=None):
    """Check DB for leadership-relevant role promotions since last snapshot."""
    changes = db.detect_role_changes(conn=conn)
    signals = []
    for change in changes:
        old_role = (change.get("old_role") or "").strip()
        new_role = (change.get("new_role") or "").strip()
        if old_role != "member" or new_role != "elder":
            continue
        signals.append({
            "type": "elder_promotion",
            "tag": change["tag"],
            "name": change["name"],
            "old_role": old_role,
            "new_role": new_role,
            "signal_log_type": change.get("signal_log_type"),
            "message": f"{change['name']} was promoted to Elder.",
        })
    return signals


def detect_donation_leaders(current_members, conn=None):
    """Identify the top 3 donors from the current roster.

    Gated on war-week day >= 3 so the ranking isn't posted right after the
    weekly donation reset when the board is near-empty. Also fires at most
    once per day.
    """
    from storage.war_calendar import war_week_day

    live_states = db.get_recent_live_war_states(limit=1, conn=conn)
    current_state = live_states[0] if live_states else None
    day = war_week_day(current_state.get("period_index")) if current_state else None
    if day is None or day < 3:
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    if db.was_signal_sent("donation_leaders", today, conn=conn):
        return []
    sorted_members = sorted(current_members, key=lambda m: m.get("donations", 0), reverse=True)
    top = sorted_members[:3]
    if not top or top[0].get("donations", 0) == 0:
        return []
    return [{
        "type": "donation_leaders",
        "leaders": [
            {"name": m.get("name", "?"), "donations": m.get("donations", 0), "rank": i + 1}
            for i, m in enumerate(top)
        ],
    }]


def detect_clan_rank_top_spot(conn=None):
    """Emit clan_rank_top_spot when a member takes over the clan-level #1 slot.

    v4.7 #29: clan_rank is already tracked on every snapshot but never
    signaled. Someone leapfrogging into the clan-leaderboard #1 slot is
    exactly the kind of durable #player-progress moment the awareness agent
    can frame; previously it passed silently.

    Compares the two most recent ``member_state_snapshots`` per active
    member. Fires when clan_rank=1 now and the previous snapshot had
    clan_rank>1 (or None). Dedups via ``signal_log_type`` keyed on tag +
    observed_at.
    """
    close = conn is None
    conn = conn or db.get_connection()
    signals = []
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT s.member_id, s.observed_at, s.clan_rank,
                       m.player_tag AS tag, m.current_name AS name,
                       ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
                WHERE m.status = 'active'
            )
            SELECT a.tag, a.name, a.observed_at, a.clan_rank AS new_rank,
                   b.clan_rank AS prev_rank
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn = 1 AND b.rn = 2
              AND a.clan_rank = 1
              AND (b.clan_rank IS NULL OR b.clan_rank > 1)
            """
        ).fetchall()
        for row in rows:
            signal_log_type = f"clan_rank_top_spot:{row['tag']}:{row['observed_at']}"
            if db.was_signal_sent_any_date(signal_log_type, conn=conn):
                continue
            signals.append({
                "type": "clan_rank_top_spot",
                "tag": row["tag"],
                "name": row["name"],
                "previous_rank": row["prev_rank"],
                "signal_log_type": signal_log_type,
            })
    finally:
        if close:
            conn.close()
    return signals


def detect_weekly_donation_leader(now=None, conn=None):
    """Emit weekly_donation_leader on Mondays with last week's top-3 donors.

    The existing ``detect_donation_leaders`` fires daily (top-3 daily donors)
    and is discardable tomorrow. A *weekly* leader is a more durable moment
    — the member who carried the donation load across the whole CR week.
    Fires once per ISO week, only when today is Monday (the day after the
    CR week closed). Uses ``member_daily_metrics`` frozen Sunday
    ``donations_week`` values.
    """
    now = now or datetime.now()
    if now.weekday() != 0:  # Monday only
        return []

    # Last Sunday's metric_date carries the final weekly donations total.
    sunday = now - timedelta(days=1)
    last_sunday = sunday.strftime("%Y-%m-%d")
    iso_year, iso_week, _ = sunday.isocalendar()
    week_key = f"{iso_year}W{iso_week:02d}"

    signal_log_type = f"weekly_donation_leader:{week_key}"

    close = conn is None
    conn = conn or db.get_connection()
    signals = []
    try:
        if db.was_signal_sent_any_date(signal_log_type, conn=conn):
            return signals

        rows = conn.execute(
            """
            SELECT m.player_tag AS tag, m.current_name AS name,
                   d.donations_week AS donations
            FROM member_daily_metrics d
            JOIN members m ON m.member_id = d.member_id
            WHERE d.metric_date = ? AND d.donations_week > 0
              AND m.status = 'active'
            ORDER BY d.donations_week DESC
            LIMIT 3
            """,
            (last_sunday,),
        ).fetchall()

        if not rows or rows[0]["donations"] == 0:
            return signals

        leaders = [
            {"name": r["name"], "tag": r["tag"], "donations": r["donations"], "rank": i + 1}
            for i, r in enumerate(rows)
        ]
        signals.append({
            "type": "weekly_donation_leader",
            "week_key": week_key,
            "week_ending": last_sunday,
            "leaders": leaders,
            "signal_log_type": signal_log_type,
        })
    finally:
        if close:
            conn.close()
    return signals


def detect_clan_score_records(conn=None):
    """Emit clan_war_trophies_record when a new all-time war-trophies high is set.

    Clan score (sum of member trophies) was removed — it reflects roster
    composition, not earned achievement. War trophies are the meaningful
    signal: they are won in battle and increase only through collective effort.

    Dedup: ``signal_log_type`` keyed on ``clan_war_trophies_record:<date>``,
    so each record only fires once (even across restarts).
    """
    close = conn is None
    conn = conn or db.get_connection()
    signals = []
    try:
        rows = conn.execute(
            """
            SELECT metric_date, clan_war_trophies
            FROM clan_daily_metrics
            WHERE clan_war_trophies IS NOT NULL
            ORDER BY metric_date
            """
        ).fetchall()

        if len(rows) < 2:
            return signals

        latest = rows[-1]
        final_date = latest["metric_date"]
        final_cwt = latest["clan_war_trophies"] or 0
        prev_max_war = max((r["clan_war_trophies"] or 0) for r in rows[:-1])

        if final_cwt > prev_max_war > 0:
            signal_log_type = f"clan_war_trophies_record:{final_date}"
            if not db.was_signal_sent_any_date(signal_log_type, conn=conn):
                signals.append({
                    "type": "clan_war_trophies_record",
                    "metric_date": final_date,
                    "previous_record": prev_max_war,
                    "new_record": final_cwt,
                    "signal_log_type": signal_log_type,
                })
    finally:
        if close:
            conn.close()
    return signals


def detect_deck_archetype_changes(now=None, conn=None):
    """Emit deck_archetype_change when a member's deck differs by 4+ cards
    from the deck they were running 24+ hours ago.

    ``member_deck_snapshots`` is populated on every battle log ingest (18k+
    rows and climbing) but nothing reads it. Leaders frequently ask "when
    did X switch decks?" — this signal answers that without them having to
    dig. The 24-hour comparison window naturally de-flickers: if a member
    swaps mid-session and swaps back, the endpoints match and no signal
    fires. Only meaningful, durable changes land.

    Uses ``mode_scope='overall'`` snapshots (stable longer window). Dedups
    via ``signal_log_type`` keyed on ``<tag>:<YYYY-MM-DD>`` so at most one
    signal per member per day.
    """
    now = now or datetime.now()
    cutoff = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    today = now.strftime("%Y-%m-%d")

    close = conn is None
    conn = conn or db.get_connection()
    signals = []
    try:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT s.member_id, s.deck_json, s.fetched_at,
                       ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.fetched_at DESC) AS rn
                FROM member_deck_snapshots s
                JOIN members m ON m.member_id = s.member_id
                WHERE s.mode_scope = 'overall' AND m.status = 'active'
            ),
            baseline AS (
                SELECT s.member_id, s.deck_json, s.fetched_at,
                       ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.fetched_at DESC) AS rn
                FROM member_deck_snapshots s
                JOIN members m ON m.member_id = s.member_id
                WHERE s.mode_scope = 'overall' AND m.status = 'active'
                  AND s.fetched_at <= ?
            )
            SELECT m.player_tag AS tag, m.current_name AS name,
                   l.deck_json AS latest_deck, l.fetched_at AS latest_at,
                   b.deck_json AS baseline_deck, b.fetched_at AS baseline_at
            FROM latest l
            JOIN baseline b ON b.member_id = l.member_id AND b.rn = 1
            JOIN members m ON m.member_id = l.member_id
            WHERE l.rn = 1
            """,
            (cutoff,),
        ).fetchall()

        for row in rows:
            try:
                latest = {c["name"] for c in json.loads(row["latest_deck"])}
                baseline = {c["name"] for c in json.loads(row["baseline_deck"])}
            except (TypeError, ValueError, KeyError):
                continue
            if not latest or not baseline:
                continue

            added = sorted(latest - baseline)
            removed = sorted(baseline - latest)
            if len(added) < DECK_SWAP_CARD_THRESHOLD:
                continue

            signal_log_type = f"deck_archetype_change:{row['tag']}:{today}"
            if db.was_signal_sent_any_date(signal_log_type, conn=conn):
                continue

            signals.append({
                "type": "deck_archetype_change",
                "tag": row["tag"],
                "name": row["name"],
                "added_cards": added,
                "removed_cards": removed,
                "changed_count": len(added),
                "latest_fetched_at": row["latest_at"],
                "baseline_fetched_at": row["baseline_at"],
                "signal_log_type": signal_log_type,
            })
    finally:
        if close:
            conn.close()
    return signals


def detect_form_slumps(conn=None):
    """Emit recent_form_slump when a member's form crosses top-tier → bottom-tier.

    v4.7 #27: ``member_recent_form`` computes nightly form labels across four
    scopes; until now only upward streaks (``battle_hot_streak``) were
    surfaced. Leaders want early notice when a reliable player goes cold —
    it's the first signal of frustration or meta drift. The agent usually
    won't post these publicly and will instead flag a leadership watch.

    Transition rule: previous label in {hot, strong} AND current label in
    {slumping, cold}. Per-(member,scope) cursor via
    ``signal_detector_cursors`` remembers the last-observed label so the
    emit fires exactly on the crossing. Weekly dedup via ``signal_log_type``
    keyed on tag + scope + isoweek.
    """
    TOP = {"hot", "strong"}
    BOTTOM = {"slumping", "cold"}
    DETECTOR_KEY = "form_slump"

    close = conn is None
    conn = conn or db.get_connection()
    signals = []
    try:
        rows = conn.execute(
            """
            SELECT m.player_tag AS tag, m.current_name AS name, f.scope,
                   f.form_label, f.sample_size, f.computed_at, f.summary
            FROM member_recent_form f
            JOIN members m ON m.member_id = f.member_id
            WHERE m.status = 'active'
              AND f.form_label IS NOT NULL
            """
        ).fetchall()

        for row in rows:
            tag = row["tag"]
            scope = row["scope"]
            new_label = row["form_label"]
            scope_key = f"{tag}:{scope}"
            cursor = db.get_signal_detector_cursor(DETECTOR_KEY, scope_key, conn=conn)
            prev_label = cursor.get("cursor_text") if cursor else None

            if prev_label != new_label:
                db.upsert_signal_detector_cursor(
                    DETECTOR_KEY, scope_key, cursor_text=new_label, conn=conn
                )

            if prev_label not in TOP or new_label not in BOTTOM:
                continue

            try:
                computed_dt = datetime.fromisoformat(
                    (row["computed_at"] or "").replace("Z", "+00:00")
                )
                year, week, _ = computed_dt.isocalendar()
                week_key = f"{year}W{week:02d}"
            except (ValueError, AttributeError):
                week_key = "unknown"

            signal_log_type = f"recent_form_slump:{tag}:{scope}:{week_key}"
            if db.was_signal_sent_any_date(signal_log_type, conn=conn):
                continue

            signals.append({
                "type": "recent_form_slump",
                "tag": tag,
                "name": row["name"],
                "scope": scope,
                "previous_label": prev_label,
                "new_label": new_label,
                "sample_size": row["sample_size"],
                "summary": row["summary"],
                "signal_log_type": signal_log_type,
            })
    finally:
        if close:
            conn.close()
    return signals


def detect_returning_members(now=None, conn=None):
    """Emit member_active_again when a previously dormant member plays again.

    v4.7 #26: watch-list memories written by the v4.6 awareness loop never had
    a natural "clear" signal. Now we emit when the most recent snapshot shows
    ``last_seen_api`` became fresh after a prior period of staleness (>= the
    inactivity threshold). The agent can then mark the watch resolved and,
    optionally, welcome the returning member.

    Uses the two most recent ``member_state_snapshots`` per active member.
    Fires at most once per member per return — a signal_log_type keyed on
    tag + the returning snapshot's observed_at.
    """
    now = now or datetime.now()
    threshold = cr_knowledge.INACTIVITY_DAYS
    close = conn is None
    conn = conn or db.get_connection()
    signals = []
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT s.*, m.player_tag AS tag, m.current_name AS name,
                       ROW_NUMBER() OVER (PARTITION BY s.member_id ORDER BY s.observed_at DESC) AS rn
                FROM member_state_snapshots s
                JOIN members m ON m.member_id = s.member_id
                WHERE m.status = 'active'
            )
            SELECT a.tag, a.name, a.last_seen_api AS new_last_seen, a.observed_at,
                   b.last_seen_api AS prev_last_seen
            FROM ranked a
            JOIN ranked b ON a.member_id = b.member_id
            WHERE a.rn = 1 AND b.rn = 2
              AND a.last_seen_api IS NOT NULL
              AND b.last_seen_api IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            try:
                new_seen = datetime.strptime(row["new_last_seen"].split(".")[0], "%Y%m%dT%H%M%S")
                prev_seen = datetime.strptime(row["prev_last_seen"].split(".")[0], "%Y%m%dT%H%M%S")
            except (ValueError, TypeError, AttributeError):
                continue
            prev_gap_days = (now - prev_seen).days
            current_gap_days = (now - new_seen).days
            # Was stale (>= threshold), now fresh (< threshold), and last_seen
            # advanced (a real new login, not a stale snapshot copy).
            if prev_gap_days < threshold:
                continue
            if current_gap_days >= threshold:
                continue
            if new_seen <= prev_seen:
                continue
            signal_log_type = f"member_active_again:{row['tag']}:{row['observed_at']}"
            if db.was_signal_sent_any_date(signal_log_type, conn=conn):
                continue
            signals.append({
                "type": "member_active_again",
                "tag": row["tag"],
                "name": row["name"],
                "days_away": prev_gap_days,
                "signal_log_type": signal_log_type,
            })
    finally:
        if close:
            conn.close()
    return signals


def detect_inactivity(current_members, now=None, conn=None):
    """Flag members past the trophy-scaled battle-inactivity threshold.

    Uses ``flag_inactive_members`` — days since last PvP or war battle
    (whichever is more recent) compared to ``max(7, trophies/1000 × 1.4)``.
    Login freshness is context; battles drive the flag. Fires only on
    Fridays, at most once per week, for the #leader-lounge report.
    """
    from storage.war_analytics import flag_inactive_members

    now = now or datetime.now()
    if now.weekday() != 4:  # 0=Mon ... 4=Fri
        return []
    today_str = now.strftime("%Y-%m-%d")
    if db.was_signal_sent("inactive_members", today_str, conn=conn):
        return []

    flagged = flag_inactive_members(today=now.date(), include_leadership=True, conn=conn)
    if not flagged:
        return []

    members = [
        {
            "name": m["name"] or "?",
            "tag": m["tag"] or "",
            "days_inactive": m["days_inactive"],
            "battle_days_ago": m["battle_days_ago"],
            "login_days_ago": m["login_days_ago"],
            "pvp_days_ago": m["pvp_days_ago"],
            "war_days_ago": m["war_days_ago"],
            "threshold_days": m["threshold_days"],
            "role": m.get("role") or "member",
            "hint": m["hint"],
        }
        for m in flagged
    ]
    return [{
        "type": "inactive_members",
        "members": members,
    }]


@db.managed_connection
def detect_cake_days(today_str=None, conn=None):
    """Check for clan birthday, join anniversaries, and member birthdays.

    Uses cake_day_announcements table for dedup -- only returns signals
    for events not yet announced today. Payloads are intentionally rich
    enough that the awareness loop / clan-events post path can compose
    a Discord post without further tool calls.

    Returns list of signal dicts.
    """
    if today_str is None:
        today_str = datetime.now().strftime("%Y-%m-%d")

    signals = []

    # Clan birthday -- founded date from config
    thresholds = prompts.thresholds()
    clan_founded = thresholds.get("clan_founded", "2026-02-04")
    if today_str[5:] == clan_founded[5:]:  # month-day match
        if not db.was_announcement_sent(today_str, "clan_birthday", None, conn=conn):
            years = int(today_str[:4]) - int(clan_founded[:4])
            clan_name_row = conn.execute(
                "SELECT clan_name FROM clan_daily_metrics "
                "WHERE clan_name IS NOT NULL "
                "ORDER BY metric_date DESC, observed_at DESC LIMIT 1",
            ).fetchone()
            member_count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM members WHERE status = 'active'",
            ).fetchone()
            signals.append({
                "type": "clan_birthday",
                "years": years,
                "founding_date": clan_founded,
                "clan_name": (clan_name_row["clan_name"] if clan_name_row else None) or "POAP KINGS",
                "active_member_count": member_count_row["n"] if member_count_row else None,
            })

    # Join anniversaries (yearly + quarterly milestones at 3/6/9 months).
    # Quarterly cadence is intentional — a year is a long time in Clash Royale
    # so we celebrate the smaller cycles too.
    anniversaries = db.get_join_anniversaries_today(today_str, conn=conn)
    unannounced = []
    today_date = datetime.strptime(today_str[:10], "%Y-%m-%d").date()
    for a in anniversaries:
        if db.was_announcement_sent(today_str, "join_anniversary", a["tag"], conn=conn):
            continue
        enriched = dict(a)
        role_row = conn.execute(
            "SELECT cs.role FROM member_current_state cs "
            "JOIN members m ON m.member_id = cs.member_id "
            "WHERE m.player_tag = ?",
            (a["tag"],),
        ).fetchone()
        enriched["role"] = role_row["role"] if role_row else None
        try:
            joined_day = datetime.strptime(a["joined_date"][:10], "%Y-%m-%d").date()
            enriched["tenure_days"] = (today_date - joined_day).days
        except (ValueError, TypeError):
            enriched["tenure_days"] = None
        unannounced.append(enriched)
    if unannounced:
        signals.append({
            "type": "join_anniversary",
            "members": unannounced,
        })

    # Member birthdays
    birthdays = db.get_birthdays_today(today_str, conn=conn)
    unannounced_bdays = []
    for b in birthdays:
        if db.was_announcement_sent(today_str, "birthday", b["tag"], conn=conn):
            continue
        enriched = dict(b)
        enrich_row = conn.execute(
            "SELECT cs.role, mm.joined_at FROM members m "
            "LEFT JOIN member_current_state cs ON cs.member_id = m.member_id "
            "LEFT JOIN member_metadata mm ON mm.member_id = m.member_id "
            "WHERE m.player_tag = ?",
            (b["tag"],),
        ).fetchone()
        if enrich_row:
            enriched["role"] = enrich_row["role"]
            joined_at = enrich_row["joined_at"]
            try:
                joined_day = datetime.strptime(joined_at[:10], "%Y-%m-%d").date()
                enriched["tenure_days"] = (today_date - joined_day).days
            except (ValueError, TypeError, AttributeError):
                enriched["tenure_days"] = None
        else:
            enriched["role"] = None
            enriched["tenure_days"] = None
        unannounced_bdays.append(enriched)
    if unannounced_bdays:
        signals.append({
            "type": "member_birthday",
            "members": unannounced_bdays,
        })

    return signals


def detect_pending_system_signals(today_str=None, conn=None):
    del today_str
    return db.list_pending_system_signals(conn=conn)
