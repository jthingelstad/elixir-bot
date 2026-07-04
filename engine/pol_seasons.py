"""Ranked season lifecycle — ranked-and-profiles.md §2.1 (D1–D6 ratified).

Mirrors the war tracker's discovered lifecycle (§16.1): the monthly reset is
OBSERVED per player (their `last` season-result swaps), never posted from the
calendar. Season ids are canonical `YYYY-MM`, named for the month the season
starts in; a season runs first-Monday to first-Monday (Jamie-confirmed).

Internal names keep the API's PathOfLegend/pol_* spelling; all display copy
says "Ranked" (the mid-2025 rename — normalize.py owns the era-aware league
names).

Close sequence (first observation wins):
1. Any ranked diff ensures the open `pol_seasons` row (cold-start self-seed).
2. The first player whose rollover is observed closes the open row and
   snapshots EVERY tracked ranked baseline into `pol_season_results` — safe
   because a not-yet-diffed baseline's `current` IS the end-of-season state,
   and the observing player's authoritative values arrive in `new["last"]`.
3. pol_champ awards (ranks 1–3, league then rating) + ONE podium summary
   intent to clan-events (`clan:` prefix routing) + the ranked chronicle.
4. Later rollover observations upsert their own result row (same values by
   construction) and emit their per-player event; the season work dedups.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

log = logging.getLogger("engine.pol_seasons")

PODIUM = 3


# ------------------------------------------------------------- season id math

def first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def season_id_for(on: date) -> str:
    """The season OPEN on `on`: named for the month of the most recent
    first-Monday ≤ `on` (a season runs first-Monday → first-Monday)."""
    fm = first_monday(on.year, on.month)
    if on >= fm:
        return f"{on.year:04d}-{on.month:02d}"
    prev_year, prev_month = (on.year, on.month - 1) if on.month > 1 else (on.year - 1, 12)
    return f"{prev_year:04d}-{prev_month:02d}"


def previous_season_id(season_id: str) -> str:
    y, m = int(season_id[:4]), int(season_id[5:7])
    return f"{y:04d}-{m - 1:02d}" if m > 1 else f"{y - 1:04d}-12"


def _obs_date(observed_at: str) -> date:
    return date(int(observed_at[:4]), int(observed_at[5:7]), int(observed_at[8:10]))


# ------------------------------------------------------------------ lifecycle

# NOTE: plain execute (not executescript — its implicit COMMIT would split
# the tick's transaction mid-step).
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pol_seasons (
        pol_season_id TEXT PRIMARY KEY,
        started_at TEXT, ended_at TEXT,
        closed INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS pol_season_results (
        pol_season_id TEXT NOT NULL REFERENCES pol_seasons(pol_season_id),
        player_tag TEXT NOT NULL,
        league INTEGER, rating INTEGER, global_rank INTEGER,
        battles INTEGER, wins INTEGER,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (pol_season_id, player_tag)
    )""",
)


def _ensure_schema(conn) -> None:
    """Lazy CREATEs (editor_verdicts pattern): live DBs gain the tables on
    first ranked observation; fresh builds get them from schema_v51."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pol_seasons'"
    ).fetchone():
        for stmt in _SCHEMA:
            conn.execute(stmt)


def ensure_open_season(conn, observed_at: str) -> str:
    """Self-seeding cold start: the first ranked observation after deploy
    births the currently-running season row (no restart-time hook needed)."""
    _ensure_schema(conn)
    sid = season_id_for(_obs_date(observed_at))
    conn.execute(
        "INSERT OR IGNORE INTO pol_seasons (pol_season_id, started_at) VALUES (?, ?)",
        (sid, observed_at),
    )
    return sid


def _open_season(conn) -> str | None:
    row = conn.execute(
        "SELECT pol_season_id FROM pol_seasons WHERE closed = 0 "
        "ORDER BY pol_season_id DESC LIMIT 1"
    ).fetchone()
    return row["pol_season_id"] if row else None


def _season_window_battles(conn, tag: str, season_id: str) -> tuple[int, int]:
    """Ranked battles/wins over the season's calendar window, from rollups."""
    y, m = int(season_id[:4]), int(season_id[5:7])
    start = first_monday(y, m).isoformat()
    ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
    end = first_monday(ny, nm).isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(battles), 0) AS b, COALESCE(SUM(wins), 0) AS w
           FROM player_daily_battle_rollups
           WHERE player_tag = ? AND mode_group = 'ranked'
             AND battle_date >= ? AND battle_date < ?""",
        (tag, start, end),
    ).fetchone()
    return row["b"], row["w"]


def _snapshot_results(conn, season_id: str, observed_at: str) -> int:
    """Write pol_season_results for every tracked ranked baseline. A baseline
    that hasn't diffed past the reset still shows end-of-season values in
    `current` — exact, not approximate (last == pre-reset current)."""
    rows = conn.execute(
        "SELECT entity_tag, payload_json FROM state_baselines "
        "WHERE entity_kind = 'player' AND aspect = 'ranked'"
    ).fetchall()
    n = 0
    for r in rows:
        try:
            p = json.loads(r["payload_json"]) or {}
        except (TypeError, ValueError):
            continue
        league, rating, rank = p.get("league"), p.get("trophies"), p.get("rank")
        if league is None and rating is None:
            continue  # never actually played ranked
        battles, wins = _season_window_battles(conn, r["entity_tag"], season_id)
        conn.execute(
            """INSERT OR IGNORE INTO pol_season_results
                   (pol_season_id, player_tag, league, rating, global_rank,
                    battles, wins, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (season_id, r["entity_tag"], league, rating, rank, battles, wins,
             observed_at),
        )
        n += 1
    return n


def _upsert_own_result(conn, season_id: str, tag: str, last: dict, observed_at: str) -> None:
    """The player's own rollover observation carries authoritative season-end
    values in `last` — overwrite the snapshot row."""
    battles, wins = _season_window_battles(conn, tag, season_id)
    conn.execute(
        """INSERT INTO pol_season_results
               (pol_season_id, player_tag, league, rating, global_rank,
                battles, wins, observed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(pol_season_id, player_tag) DO UPDATE SET
               league = excluded.league, rating = excluded.rating,
               global_rank = excluded.global_rank, battles = excluded.battles,
               wins = excluded.wins, observed_at = excluded.observed_at""",
        (season_id, tag, last.get("league"), last.get("trophies"),
         last.get("rank"), battles, wins, observed_at),
    )


def podium(conn, season_id: str) -> list[dict]:
    """Top-3 by league then rating (ranked-and-profiles.md §2.2), active
    members only, minimum one ranked battle in the season window."""
    from engine.normalize import ranked_league_name

    rows = conn.execute(
        """SELECT r.player_tag, r.league, r.rating, r.global_rank,
                  r.battles, r.wins, p.current_name
           FROM pol_season_results r
           JOIN players p ON p.player_tag = r.player_tag
           JOIN clan_memberships cm ON cm.player_tag = r.player_tag
                AND cm.left_at IS NULL
           WHERE r.pol_season_id = ? AND r.league IS NOT NULL AND r.battles > 0
           ORDER BY r.league DESC, COALESCE(r.rating, 0) DESC, r.player_tag
           LIMIT ?""",
        (season_id, PODIUM),
    ).fetchall()
    return [
        {"rank": i + 1, "tag": r["player_tag"], "name": r["current_name"],
         "league": r["league"], "league_name": ranked_league_name(r["league"]),
         "rating": r["rating"], "global_rank": r["global_rank"],
         "battles": r["battles"], "wins": r["wins"]}
        for i, r in enumerate(rows)
    ]


def _close_season_once(conn, season_id: str, observed_at: str) -> None:
    """Season-level close work — runs exactly once (guarded by the closed
    flag flip). Awards + podium intent + chronicle, each unable to lose the
    close itself."""
    cur = conn.execute(
        "UPDATE pol_seasons SET closed = 1, ended_at = ? "
        "WHERE pol_season_id = ? AND closed = 0",
        (observed_at, season_id),
    )
    if not cur.rowcount:
        return  # someone else closed it (idempotence)
    snapshot = _snapshot_results(conn, season_id, observed_at)
    log.info("ranked season %s closed; %s results snapshotted", season_id, snapshot)
    try:
        from engine import delivery
        from engine.recognition import ledger

        pod = podium(conn, season_id)
        for entry in pod:
            if conn.execute(
                """INSERT OR IGNORE INTO awards
                       (award_type, season_id, section_index, player_tag, rank,
                        metric_value, metric_unit, metadata_json, awarded_at)
                   VALUES ('pol_champ', ?, -1, ?, ?, ?, 'rating', ?, ?)""",
                # awards.season_id is INTEGER (war); ranked ids are 'YYYY-MM' —
                # store as the sortable integer YYYYMM, keep the text id in
                # metadata (the awards UNIQUE constraint still dedups).
                (int(season_id.replace("-", "")), entry["tag"], entry["rank"],
                 entry["rating"],
                 json.dumps({"pol_season_id": season_id,
                             "league": entry["league"],
                             "league_name": entry["league_name"],
                             "battles": entry["battles"], "wins": entry["wins"]}),
                 observed_at),
            ).rowcount:
                ledger.claim(
                    conn, f"award:pol_champ:{season_id}:{entry['tag']}", "player",
                    [f"pol_season_closed:{season_id}"], 0,
                )
        if pod and ledger.claim(
            conn, f"pol_season:{season_id}", "player",
            [f"pol_season_closed:{season_id}"], 0,
        ):
            intent_id = delivery.raise_intent(
                conn, f"pol_season:{season_id}", "clan:pol_season_podium",
                "clan-events", "public",
                {"event_type": "pol_season_podium", "pol_season_id": season_id,
                 "podium": pod}, observed_at,
            )
            ledger.attach_intent(conn, f"pol_season:{season_id}", intent_id)
    except Exception:
        log.exception("ranked podium/awards failed for %s", season_id)
    try:
        from engine import chronicles

        chronicles.write_season_chronicle(conn, "ranked", season_id, observed_at)
    except Exception:
        log.exception("ranked chronicle failed for %s", season_id)


def observe_rollover(conn, tag: str, old: dict, new: dict, observed_at: str) -> int:
    """A player's ranked baseline showed the reset signature. Emit their
    per-player event, close the season (first observation wins), upsert their
    authoritative result row, and open the new season."""
    from engine.emitters import insert_stream_event

    closing = _open_season(conn)
    new_sid = season_id_for(_obs_date(observed_at))
    if closing is None or closing == new_sid:
        # cold path: rollover observed before any pre-reset tick seeded the
        # old season (deploy raced the reset) — derive the closed id.
        closing = previous_season_id(new_sid)
        conn.execute(
            "INSERT OR IGNORE INTO pol_seasons (pol_season_id, started_at) VALUES (?, ?)",
            (closing, observed_at),
        )
    _close_season_once(conn, closing, observed_at)
    _upsert_own_result(conn, closing, tag, new.get("last") or {}, observed_at)
    conn.execute(
        "INSERT OR IGNORE INTO pol_seasons (pol_season_id, started_at) VALUES (?, ?)",
        (new_sid, observed_at),
    )
    last = new.get("last") or {}
    return insert_stream_event(
        conn,
        "player_events",
        dedup_key=f"pol_season_closed:{tag}:{closing}",
        event_type="pol_season_closed",
        subject_cols={"player_tag": tag},
        observed_at=observed_at,
        window_start=None,
        payload={"pol_season_id": closing, "league": last.get("league"),
                 "rating": last.get("trophies"), "global_rank": last.get("rank")},
    )
