"""db.py — SQLite history store for Elixir bot.

Tracks member snapshots over time, war results, and participation.
Self-managing: purges data older than retention thresholds.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from cr_knowledge import TROPHY_MILESTONES

log = logging.getLogger("elixir_db")

DB_PATH = os.getenv("ELIXIR_DB_PATH", os.path.join(os.path.dirname(__file__), "elixir.db"))

# Retention periods
SNAPSHOT_RETENTION_DAYS = 90
WAR_RETENTION_DAYS = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS member_snapshots (
    id INTEGER PRIMARY KEY,
    tag TEXT NOT NULL,
    name TEXT,
    trophies INTEGER,
    best_trophies INTEGER,
    donations INTEGER,
    donations_received INTEGER,
    role TEXT,
    arena_id INTEGER,
    arena_name TEXT,
    exp_level INTEGER,
    clan_rank INTEGER,
    last_seen TEXT,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_snapshots_tag_time ON member_snapshots(tag, recorded_at);

CREATE TABLE IF NOT EXISTS war_results (
    id INTEGER PRIMARY KEY,
    season_id INTEGER,
    section_index INTEGER,
    our_rank INTEGER,
    our_fame INTEGER,
    finish_time TEXT,
    created_date TEXT,
    standings_json TEXT,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(season_id, section_index)
);

CREATE TABLE IF NOT EXISTS war_participation (
    id INTEGER PRIMARY KEY,
    war_result_id INTEGER REFERENCES war_results(id),
    tag TEXT NOT NULL,
    name TEXT,
    fame INTEGER,
    repair_points INTEGER,
    decks_used INTEGER,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_war_part_tag ON war_participation(tag);

CREATE TABLE IF NOT EXISTS leader_conversations (
    id INTEGER PRIMARY KEY,
    author_id TEXT NOT NULL,
    author_name TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_leader_conv_author ON leader_conversations(author_id, recorded_at);

CREATE TABLE IF NOT EXISTS member_dates (
    tag TEXT PRIMARY KEY,
    name TEXT,
    joined_date TEXT,
    birth_month INTEGER,
    birth_day INTEGER,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
);

CREATE TABLE IF NOT EXISTS cake_day_announcements (
    id INTEGER PRIMARY KEY,
    announcement_date TEXT NOT NULL,
    announcement_type TEXT NOT NULL,
    target_tag TEXT,
    recorded_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    UNIQUE(announcement_date, announcement_type, target_tag)
);
"""

CONVERSATION_RETENTION_DAYS = 30
CONVERSATION_MAX_PER_LEADER = 20


def get_connection(db_path=None):
    """Get a SQLite connection, creating schema if needed."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def snapshot_members(member_list, conn=None):
    """Save a snapshot only for members whose data has changed.

    Compares each member to their most recent snapshot. Only inserts a
    new row when trophies, donations, role, arena, or clan_rank differ.
    This keeps the DB lean and makes history queries return just the changes.

    member_list: list of dicts from CR API memberList.
    Returns the number of changed members that were actually stored.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        # Fetch the latest snapshot for each member
        latest = {}
        for row in conn.execute(
            "SELECT * FROM member_snapshots WHERE id IN "
            "(SELECT MAX(id) FROM member_snapshots GROUP BY tag)"
        ).fetchall():
            latest[row["tag"]] = dict(row)

        stored = 0
        for m in member_list:
            arena = m.get("arena", {})
            if isinstance(arena, dict):
                arena_id = arena.get("id")
                arena_name = arena.get("name", "")
            else:
                arena_id = None
                arena_name = str(arena) if arena else ""

            tag = m.get("tag", "")
            trophies = m.get("trophies", 0)
            donations = m.get("donations", 0)
            donations_received = m.get("donationsReceived", m.get("donations_received", 0))
            role = m.get("role", "member")
            clan_rank = m.get("clanRank", m.get("clan_rank"))

            # Check if anything meaningful changed
            prev = latest.get(tag)
            if prev is not None:
                if (
                    prev["trophies"] == trophies
                    and prev["donations"] == donations
                    and prev["role"] == role
                    and prev["arena_name"] == arena_name
                    and prev["clan_rank"] == clan_rank
                ):
                    continue  # No change, skip

            conn.execute(
                "INSERT INTO member_snapshots "
                "(tag, name, trophies, best_trophies, donations, donations_received, "
                "role, arena_id, arena_name, exp_level, clan_rank, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tag,
                    m.get("name", ""),
                    trophies,
                    m.get("bestTrophies", m.get("best_trophies")),
                    donations,
                    donations_received,
                    role,
                    arena_id,
                    arena_name,
                    m.get("expLevel", m.get("exp_level")),
                    clan_rank,
                    m.get("lastSeen", m.get("last_seen", "")),
                ),
            )
            stored += 1

        conn.commit()
        log.info("Snapshotted %d changed members (of %d total)", stored, len(member_list))
    finally:
        if close:
            conn.close()


def get_known_roster(conn=None):
    """Return {tag: name} for the most recent snapshot of each member.

    Used for join/leave detection — call BEFORE snapshot_members() in tick().
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT tag, name FROM member_snapshots WHERE id IN "
            "(SELECT MAX(id) FROM member_snapshots GROUP BY tag)"
        ).fetchall()
        return {r["tag"]: r["name"] for r in rows}
    finally:
        if close:
            conn.close()


def purge_old_data(conn=None):
    """Delete data older than retention thresholds."""
    close = conn is None
    conn = conn or get_connection()
    try:
        snap_cutoff = (datetime.utcnow() - timedelta(days=SNAPSHOT_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        war_cutoff = (datetime.utcnow() - timedelta(days=WAR_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

        cur = conn.execute(
            "DELETE FROM member_snapshots WHERE recorded_at < ?", (snap_cutoff,)
        )
        snap_deleted = cur.rowcount

        # Delete participation for old wars first (FK)
        conn.execute(
            "DELETE FROM war_participation WHERE war_result_id IN "
            "(SELECT id FROM war_results WHERE recorded_at < ?)",
            (war_cutoff,),
        )
        cur = conn.execute(
            "DELETE FROM war_results WHERE recorded_at < ?", (war_cutoff,)
        )
        war_deleted = cur.rowcount

        # Purge old conversations
        conv_cutoff = (datetime.utcnow() - timedelta(days=CONVERSATION_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        cur = conn.execute(
            "DELETE FROM leader_conversations WHERE recorded_at < ?", (conv_cutoff,)
        )
        conv_deleted = cur.rowcount

        # Purge old cake day announcements (>7 days)
        cake_cutoff = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        conn.execute(
            "DELETE FROM cake_day_announcements WHERE announcement_date < ?",
            (cake_cutoff,),
        )

        conn.commit()
        if snap_deleted or war_deleted or conv_deleted:
            log.info(
                "Purged %d old snapshots, %d old war results, %d old conversations",
                snap_deleted, war_deleted, conv_deleted,
            )
    finally:
        if close:
            conn.close()


def get_member_history(tag, days=30, conn=None):
    """Get a member's snapshot history over the past N days.

    Returns list of dicts with trophies, donations, role, arena, etc.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = conn.execute(
            "SELECT * FROM member_snapshots WHERE tag = ? AND recorded_at >= ? "
            "ORDER BY recorded_at ASC",
            (tag, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_trophy_changes(since_hours=24, conn=None):
    """Find members with the biggest trophy changes in the last N hours.

    Returns list of dicts: {tag, name, old_trophies, new_trophies, change}.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=since_hours)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        # Get earliest and latest snapshot for each member in the window
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT tag, name, trophies, recorded_at,
                    ROW_NUMBER() OVER (PARTITION BY tag ORDER BY recorded_at ASC) as rn_asc,
                    ROW_NUMBER() OVER (PARTITION BY tag ORDER BY recorded_at DESC) as rn_desc
                FROM member_snapshots
                WHERE recorded_at >= ?
            )
            SELECT
                a.tag, a.name,
                a.trophies AS old_trophies,
                b.trophies AS new_trophies,
                (b.trophies - a.trophies) AS change
            FROM ranked a
            JOIN ranked b ON a.tag = b.tag
            WHERE a.rn_asc = 1 AND b.rn_desc = 1 AND a.trophies != b.trophies
            ORDER BY ABS(b.trophies - a.trophies) DESC
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def detect_milestones(conn=None):
    """Compare the two most recent snapshots for each member.

    Detects:
    - Trophy milestones crossed (5k, 6k, 7k, 8k, 9k, 10k, 12k)
    - Arena changes

    Returns list of dicts: {tag, name, type, old_value, new_value, milestone?}.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        # Get the two most recent distinct snapshot times
        times = conn.execute(
            "SELECT DISTINCT recorded_at FROM member_snapshots "
            "ORDER BY recorded_at DESC LIMIT 2"
        ).fetchall()
        if len(times) < 2:
            return []

        latest_time = times[0]["recorded_at"]
        prev_time = times[1]["recorded_at"]

        latest = {
            r["tag"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM member_snapshots WHERE recorded_at = ?", (latest_time,)
            ).fetchall()
        }
        prev = {
            r["tag"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM member_snapshots WHERE recorded_at = ?", (prev_time,)
            ).fetchall()
        }

        milestones = []
        for tag, curr in latest.items():
            old = prev.get(tag)
            if not old:
                continue

            # Trophy milestones
            old_t = old.get("trophies", 0) or 0
            new_t = curr.get("trophies", 0) or 0
            for threshold in TROPHY_MILESTONES:
                if old_t < threshold <= new_t:
                    milestones.append({
                        "tag": tag,
                        "name": curr.get("name", ""),
                        "type": "trophy_milestone",
                        "old_value": old_t,
                        "new_value": new_t,
                        "milestone": threshold,
                    })

            # Arena changes
            old_arena = old.get("arena_name", "")
            new_arena = curr.get("arena_name", "")
            if old_arena and new_arena and old_arena != new_arena:
                milestones.append({
                    "tag": tag,
                    "name": curr.get("name", ""),
                    "type": "arena_change",
                    "old_value": old_arena,
                    "new_value": new_arena,
                })

        return milestones
    finally:
        if close:
            conn.close()


def detect_role_changes(conn=None):
    """Detect role changes between the two most recent snapshots.

    Returns list of dicts: {tag, name, old_role, new_role}.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        times = conn.execute(
            "SELECT DISTINCT recorded_at FROM member_snapshots "
            "ORDER BY recorded_at DESC LIMIT 2"
        ).fetchall()
        if len(times) < 2:
            return []

        latest_time = times[0]["recorded_at"]
        prev_time = times[1]["recorded_at"]

        latest = {
            r["tag"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM member_snapshots WHERE recorded_at = ?", (latest_time,)
            ).fetchall()
        }
        prev = {
            r["tag"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM member_snapshots WHERE recorded_at = ?", (prev_time,)
            ).fetchall()
        }

        changes = []
        for tag, curr in latest.items():
            old = prev.get(tag)
            if not old:
                continue
            if old.get("role") != curr.get("role"):
                changes.append({
                    "tag": tag,
                    "name": curr.get("name", ""),
                    "old_role": old.get("role"),
                    "new_role": curr.get("role"),
                })
        return changes
    finally:
        if close:
            conn.close()


def store_war_log(race_log, clan_tag, conn=None):
    """Ingest river race log from CR API. Skips already-stored entries.

    race_log: dict from cr_api.get_river_race_log() with 'items' key.
    clan_tag: our clan tag (e.g. '#J2RGCRVG') to identify our standings.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        items = race_log.get("items", []) if race_log else []
        stored = 0
        for entry in items:
            season_id = entry.get("seasonId")
            section_index = entry.get("sectionIndex")
            created_date = entry.get("createdDate", "")
            standings = entry.get("standings", [])

            # Find our clan in standings
            our_standing = None
            for s in standings:
                clan = s.get("clan", {})
                if clan.get("tag", "").replace("#", "") == clan_tag.replace("#", ""):
                    our_standing = s
                    break

            our_rank = our_standing.get("rank") if our_standing else None
            our_fame = (
                our_standing.get("clan", {}).get("fame") if our_standing else None
            )
            finish_time = (
                our_standing.get("clan", {}).get("finishTime") if our_standing else None
            )

            try:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO war_results "
                    "(season_id, section_index, our_rank, our_fame, finish_time, "
                    "created_date, standings_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        season_id,
                        section_index,
                        our_rank,
                        our_fame,
                        finish_time,
                        created_date,
                        json.dumps(standings),
                    ),
                )
                if cur.rowcount == 0:
                    continue  # Already stored

                war_result_id = cur.lastrowid
                stored += 1

                # Store participant data if available
                if our_standing:
                    participants = our_standing.get("clan", {}).get("participants", [])
                    for p in participants:
                        conn.execute(
                            "INSERT INTO war_participation "
                            "(war_result_id, tag, name, fame, repair_points, decks_used) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                war_result_id,
                                p.get("tag", ""),
                                p.get("name", ""),
                                p.get("fame", 0),
                                p.get("repairPoints", 0),
                                p.get("decksUsed", 0),
                            ),
                        )
            except sqlite3.IntegrityError:
                continue

        conn.commit()
        if stored:
            log.info("Stored %d new war results", stored)
    finally:
        if close:
            conn.close()


def get_war_history(n=10, conn=None):
    """Get the last N war results.

    Returns list of dicts with rank, fame, date, etc.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM war_results ORDER BY created_date DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_member_war_stats(tag, conn=None):
    """Get a member's war participation history.

    Returns list of dicts with fame, decks_used, and the war result context.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            """
            SELECT wp.*, wr.season_id, wr.section_index, wr.our_rank, wr.created_date
            FROM war_participation wp
            JOIN war_results wr ON wp.war_result_id = wr.id
            WHERE wp.tag = ?
            ORDER BY wr.created_date DESC
            """,
            (tag,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_promotion_candidates(conn=None):
    """Find members with 'member' role who show strong activity.

    Looks at the most recent snapshot for role, then checks:
    - donations in latest snapshot >= 50
    - active in last 7 days (last_seen)
    - war participation in at least 1 recent war

    Returns list of dicts with member info and stats.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        # Get latest snapshot time
        latest = conn.execute(
            "SELECT MAX(recorded_at) as t FROM member_snapshots"
        ).fetchone()
        if not latest or not latest["t"]:
            return []

        latest_time = latest["t"]
        seven_days_ago = (
            datetime.strptime(latest_time, "%Y-%m-%dT%H:%M:%S") - timedelta(days=7)
        ).strftime("%Y%m%dT%H%M%S")

        # Members with role='member' in latest snapshot
        members = conn.execute(
            "SELECT * FROM member_snapshots WHERE recorded_at = ? AND role = 'member'",
            (latest_time,),
        ).fetchall()

        candidates = []
        for m in members:
            tag = m["tag"]
            # Check donations
            if (m["donations"] or 0) < 50:
                continue
            # Check last_seen within 7 days
            last_seen = m["last_seen"] or ""
            # CR API lastSeen format: 20250101T120000.000Z — strip to comparable
            last_seen_clean = last_seen.replace(".", "").replace("Z", "")[:15]
            if last_seen_clean and last_seen_clean < seven_days_ago:
                continue
            # Check war participation
            war_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM war_participation WHERE tag = ? AND fame > 0",
                (tag,),
            ).fetchone()["cnt"]

            candidates.append({
                **dict(m),
                "war_participations": war_count,
            })

        return candidates
    finally:
        if close:
            conn.close()


# ── Conversation memory ──────────────────────────────────────────────────────

def save_conversation_turn(author_id, author_name, role, content, conn=None):
    """Save a single conversation turn (question or response).

    role: 'user' for the leader's question, 'assistant' for Elixir's response.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT INTO leader_conversations (author_id, author_name, role, content) "
            "VALUES (?, ?, ?, ?)",
            (author_id, author_name, role, content),
        )
        # Trim to keep only the most recent turns per leader
        conn.execute(
            "DELETE FROM leader_conversations WHERE id NOT IN "
            "(SELECT id FROM leader_conversations WHERE author_id = ? "
            "ORDER BY recorded_at DESC LIMIT ?) AND author_id = ?",
            (author_id, CONVERSATION_MAX_PER_LEADER, author_id),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def get_conversation_history(author_id, limit=10, conn=None):
    """Get recent conversation turns for a leader.

    Returns list of dicts: {role, content, recorded_at}, oldest first.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(
            "SELECT role, content, recorded_at FROM leader_conversations "
            "WHERE author_id = ? ORDER BY recorded_at DESC LIMIT ?",
            (author_id, limit),
        ).fetchall()
        # Return oldest first so they read chronologically
        return [dict(r) for r in reversed(rows)]
    finally:
        if close:
            conn.close()


def purge_old_conversations(conn=None):
    """Delete conversation turns older than retention period."""
    close = conn is None
    conn = conn or get_connection()
    try:
        cutoff = (datetime.utcnow() - timedelta(days=CONVERSATION_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        conn.execute("DELETE FROM leader_conversations WHERE recorded_at < ?", (cutoff,))
        conn.commit()
    finally:
        if close:
            conn.close()


# ── War Champ tracking ───────────────────────────────────────────────────────

def get_war_champ_standings(season_id=None, conn=None):
    """Get War Champ rankings — total fame per member across a season.

    If season_id is None, uses the most recent season.
    Returns list of dicts: {tag, name, total_fame, races_participated, avg_fame},
    sorted by total_fame descending.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            row = conn.execute(
                "SELECT MAX(season_id) as sid FROM war_results"
            ).fetchone()
            if not row or row["sid"] is None:
                return []
            season_id = row["sid"]

        rows = conn.execute(
            """
            SELECT
                wp.tag,
                wp.name,
                SUM(wp.fame) AS total_fame,
                COUNT(*) AS races_participated,
                ROUND(AVG(wp.fame), 0) AS avg_fame
            FROM war_participation wp
            JOIN war_results wr ON wp.war_result_id = wr.id
            WHERE wr.season_id = ? AND wp.fame > 0
            GROUP BY wp.tag
            ORDER BY total_fame DESC
            """,
            (season_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_current_season_id(conn=None):
    """Get the most recent season_id from war results."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(season_id) as sid FROM war_results"
        ).fetchone()
        return row["sid"] if row else None
    finally:
        if close:
            conn.close()


def get_perfect_war_participants(season_id=None, conn=None):
    """Find members who participated in every war race of a season.

    A "perfect" participant used decks in every single race the clan played
    that season. Returns list of dicts: {tag, name, races_participated,
    total_fame, total_races_in_season}.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        if season_id is None:
            row = conn.execute(
                "SELECT MAX(season_id) as sid FROM war_results"
            ).fetchone()
            if not row or row["sid"] is None:
                return []
            season_id = row["sid"]

        # How many races happened this season?
        total_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM war_results WHERE season_id = ?",
            (season_id,),
        ).fetchone()
        total_races = total_row["cnt"] if total_row else 0
        if total_races == 0:
            return []

        # Members who participated in every race
        rows = conn.execute(
            """
            SELECT
                wp.tag,
                wp.name,
                COUNT(*) AS races_participated,
                SUM(wp.fame) AS total_fame
            FROM war_participation wp
            JOIN war_results wr ON wp.war_result_id = wr.id
            WHERE wr.season_id = ? AND wp.fame > 0
            GROUP BY wp.tag
            HAVING COUNT(*) = ?
            ORDER BY total_fame DESC
            """,
            (season_id, total_races),
        ).fetchall()

        return [
            {**dict(r), "total_races_in_season": total_races}
            for r in rows
        ]
    finally:
        if close:
            conn.close()


# ── Cake day tracking ─────────────────────────────────────────────────────

def backfill_join_dates(conn=None):
    """Backfill joined_date from earliest member_snapshots for members missing it."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            """
            INSERT INTO member_dates (tag, name, joined_date)
            SELECT ms.tag, ms.name, DATE(MIN(ms.recorded_at))
            FROM member_snapshots ms
            LEFT JOIN member_dates md ON ms.tag = md.tag
            WHERE md.tag IS NULL OR md.joined_date IS NULL
            GROUP BY ms.tag
            ON CONFLICT(tag) DO UPDATE SET
                joined_date = excluded.joined_date
            WHERE member_dates.joined_date IS NULL
            """
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def record_join_date(tag, name, joined_date, conn=None):
    """Record a join date — only sets if not already present."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT INTO member_dates (tag, name, joined_date) VALUES (?, ?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET "
            "joined_date = excluded.joined_date, name = COALESCE(excluded.name, member_dates.name) "
            "WHERE member_dates.joined_date IS NULL",
            (tag, name, joined_date),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_join_date(tag, name, joined_date, conn=None):
    """Set or override a member's join date (leader override)."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT INTO member_dates (tag, name, joined_date) VALUES (?, ?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET "
            "joined_date = excluded.joined_date, name = COALESCE(excluded.name, member_dates.name)",
            (tag, name, joined_date),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def set_member_birthday(tag, name, month, day, conn=None):
    """Set or override a member's birthday (month and day)."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT INTO member_dates (tag, name, birth_month, birth_day) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET "
            "birth_month = excluded.birth_month, birth_day = excluded.birth_day, "
            "name = COALESCE(excluded.name, member_dates.name)",
            (tag, name, month, day),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def get_member_dates(tag, conn=None):
    """Get a member's dates record, or None."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM member_dates WHERE tag = ?", (tag,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        if close:
            conn.close()


def get_join_anniversaries_today(today_str, conn=None):
    """Get members whose join anniversary is today (excluding current-year joins).

    today_str: 'YYYY-MM-DD'
    Returns list of dicts with tag, name, joined_date, years.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        month_day = today_str[5:]  # 'MM-DD'
        year = today_str[:4]
        rows = conn.execute(
            "SELECT tag, name, joined_date FROM member_dates "
            "WHERE strftime('%m-%d', joined_date) = ? "
            "AND strftime('%Y', joined_date) != ? "
            "AND joined_date IS NOT NULL",
            (month_day, year),
        ).fetchall()
        result = []
        for r in rows:
            joined_year = int(r["joined_date"][:4])
            years = int(year) - joined_year
            result.append({
                "tag": r["tag"],
                "name": r["name"],
                "joined_date": r["joined_date"],
                "years": years,
            })
        return result
    finally:
        if close:
            conn.close()


def get_birthdays_today(today_str, conn=None):
    """Get members whose birthday is today.

    today_str: 'YYYY-MM-DD'
    Returns list of dicts with tag, name, birth_month, birth_day.
    """
    close = conn is None
    conn = conn or get_connection()
    try:
        month = int(today_str[5:7])
        day = int(today_str[8:10])
        rows = conn.execute(
            "SELECT tag, name, birth_month, birth_day FROM member_dates "
            "WHERE birth_month = ? AND birth_day = ?",
            (month, day),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def mark_announcement_sent(date_str, announcement_type, target_tag, conn=None):
    """Record that a cake day announcement was sent (dedup)."""
    close = conn is None
    conn = conn or get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO cake_day_announcements "
            "(announcement_date, announcement_type, target_tag) VALUES (?, ?, ?)",
            (date_str, announcement_type, target_tag),
        )
        conn.commit()
    finally:
        if close:
            conn.close()


def was_announcement_sent(date_str, announcement_type, target_tag, conn=None):
    """Check if a cake day announcement was already sent today."""
    close = conn is None
    conn = conn or get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM cake_day_announcements "
            "WHERE announcement_date = ? AND announcement_type = ? AND target_tag IS ?",
            (date_str, announcement_type, target_tag),
        ).fetchone()
        return row is not None
    finally:
        if close:
            conn.close()
