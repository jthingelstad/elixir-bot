"""v5.1 migration parity checks (docs/v5.1/migration.md Phase 6).

Two modes:

  baseline  — run the OLD-schema side against the current/archive DB and print
              every expected value. Phase 0 requires this to run clean on the
              current schema BEFORE the cut, so the queries are proven against
              known data.
  verify    — run both sides (archive vs new DB) and print expected-vs-actual
              per check. Usable from Phase 3 onward.

Usage:
    ./venv/bin/python scripts/migrate_v51/parity_checks.py baseline --old elixir-v5.db
    ./venv/bin/python scripts/migrate_v51/parity_checks.py verify \
        --old elixir-v5-archive-2026H2.db --new elixir-v51.db

Checks (migration.md Phase 6):
  identity, links, tenure, awards, war_history, rollups, calendar_seed.
The "Q&A smoke" check is runtime-level (every coverage-matrix tool aspect via
the bot's tool layer) and lives outside this script — run it at Phase 6 per
the matrix in docs/v5.1/schema.md §9.

Calendar-detection types must match migration T14 and events.md §4.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys

CALENDAR_TYPES = (
    "member_birthday",
    "clan_birthday",
    "join_anniversary",
    "weekly_donation_leader",
)
ROLLUP_SAMPLE_SIZE = 10
ROLLUP_SAMPLE_SEED = 51  # deterministic sample; same tags on every run


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------- old side

def old_identity(old):
    member_count = _one(old, "SELECT COUNT(*) FROM members")
    unresolved_open = _one(
        old,
        """SELECT COUNT(*) FROM clan_memberships cm
           LEFT JOIN members m ON m.member_id = cm.member_id
           WHERE cm.left_at IS NULL AND m.player_tag IS NULL""",
    )
    return {"players": member_count, "unresolved_open_memberships": unresolved_open}


def old_links(old):
    rows = _rows(
        old,
        """SELECT dl.discord_user_id, m.player_tag, dl.confidence
           FROM discord_links dl JOIN members m ON m.member_id = dl.member_id
           ORDER BY dl.discord_user_id, m.player_tag""",
    )
    return {"count": len(rows), "entries": [tuple(r) for r in rows]}


def old_tenure(old):
    rows = _rows(
        old,
        """SELECT m.player_tag, cm.joined_at, cm.left_at
           FROM clan_memberships cm JOIN members m ON m.member_id = cm.member_id
           ORDER BY m.player_tag, cm.joined_at""",
    )
    return {"count": len(rows), "spans": [tuple(r) for r in rows]}


def old_awards(old):
    rows = _rows(
        old,
        """SELECT award_type, season_id, COUNT(*) AS n FROM awards
           GROUP BY award_type, season_id ORDER BY award_type, season_id""",
    )
    # war_champ is a podium (ranks 1-3 per season); the free_pass seed is
    # one row per season from rank 1 only (T6, Q2 erratum).
    rank1_champs = _one(
        old, "SELECT COUNT(*) FROM awards WHERE award_type = 'war_champ' AND rank = 1"
    )
    return {
        "per_type_season": [tuple(r) for r in rows],
        "rank1_war_champ_rows": rank1_champs,  # verify: new free_pass count == this
    }


def old_war_history(old):
    rows = _rows(
        old,
        """SELECT season_id, COUNT(*) AS weeks,
                  (SELECT our_rank FROM war_races r2
                   WHERE r2.season_id = r.season_id
                   ORDER BY section_index DESC LIMIT 1) AS final_rank
           FROM war_races r GROUP BY season_id ORDER BY season_id""",
    )
    return {"seasons": [tuple(r) for r in rows], "season_count": len(rows)}


def _sample_tags(old):
    tags = [r[0] for r in _rows(old, "SELECT player_tag FROM members ORDER BY player_tag")]
    rng = random.Random(ROLLUP_SAMPLE_SEED)
    return sorted(rng.sample(tags, min(ROLLUP_SAMPLE_SIZE, len(tags))))


def old_rollups(old):
    out = {}
    for tag in _sample_tags(old):
        metrics = _rows(
            old,
            """SELECT substr(d.metric_date, 1, 7) AS month, COUNT(*),
                      COALESCE(SUM(d.trophies), 0), COALESCE(SUM(d.donations_week), 0)
               FROM member_daily_metrics d JOIN members m ON m.member_id = d.member_id
               WHERE m.player_tag = ? GROUP BY month ORDER BY month""",
            (tag,),
        )
        battles = _rows(
            old,
            """SELECT substr(b.battle_date, 1, 7) AS month,
                      COALESCE(SUM(b.battles), 0), COALESCE(SUM(b.wins), 0)
               FROM member_daily_battle_rollups b JOIN members m ON m.member_id = b.member_id
               WHERE m.player_tag = ? GROUP BY month ORDER BY month""",
            (tag,),
        )
        out[tag] = {"metrics": [tuple(r) for r in metrics], "battles": [tuple(r) for r in battles]}
    return out


def old_calendar_seed(old):
    """Dedup keys T14 must claim: calendar detections in the trailing 14 days."""
    rows = _rows(
        old,
        f"""SELECT dedup_key FROM detections
            WHERE detection_type IN ({','.join('?' * len(CALENDAR_TYPES))})
              AND occurred_at >= strftime('%Y%m%dT%H%M%S', 'now', '-14 days')
            ORDER BY dedup_key""",
        CALENDAR_TYPES,
    )
    return {"keys": [r[0] for r in rows]}


# ---------------------------------------------------------------- new side

def new_identity(new):
    return {
        "players": _one(new, "SELECT COUNT(*) FROM players"),
        "unresolved_open_memberships": _one(
            new,
            """SELECT COUNT(*) FROM clan_memberships cm
               LEFT JOIN players p ON p.player_tag = cm.player_tag
               WHERE cm.left_at IS NULL AND p.player_tag IS NULL""",
        ),
    }


def new_links(new):
    rows = _rows(
        new,
        """SELECT discord_user_id, player_tag, confidence FROM discord_links
           ORDER BY discord_user_id, player_tag""",
    )
    return {"count": len(rows), "entries": [tuple(r) for r in rows]}


def new_tenure(new):
    rows = _rows(
        new,
        """SELECT player_tag, joined_at, left_at FROM clan_memberships
           ORDER BY player_tag, joined_at""",
    )
    return {"count": len(rows), "spans": [tuple(r) for r in rows]}


def new_awards(new):
    rows = _rows(
        new,
        """SELECT award_type, season_id, COUNT(*) AS n FROM awards
           WHERE award_type != 'free_pass'
           GROUP BY award_type, season_id ORDER BY award_type, season_id""",
    )
    free_pass = _one(new, "SELECT COUNT(*) FROM awards WHERE award_type = 'free_pass'")
    return {"per_type_season": [tuple(r) for r in rows], "rank1_war_champ_rows": free_pass}
    # rank1_war_champ_rows key holds the free_pass count so the comparator lines
    # up: T6 seeds exactly one free_pass row per archived rank-1 war_champ row.


def new_war_history(new):
    rows = _rows(
        new,
        """SELECT season_id, weeks, final_rank FROM war_seasons ORDER BY season_id""",
    )
    return {"seasons": [tuple(r) for r in rows], "season_count": len(rows)}


def new_rollups(new, tags):
    out = {}
    for tag in tags:
        metrics = _rows(
            new,
            """SELECT substr(metric_date, 1, 7) AS month, COUNT(*),
                      COALESCE(SUM(trophies), 0), COALESCE(SUM(donations_week), 0)
               FROM player_daily_metrics WHERE player_tag = ?
               GROUP BY month ORDER BY month""",
            (tag,),
        )
        battles = _rows(
            new,
            """SELECT substr(battle_date, 1, 7) AS month,
                      COALESCE(SUM(battles), 0), COALESCE(SUM(wins), 0)
               FROM player_daily_battle_rollups WHERE player_tag = ?
               GROUP BY month ORDER BY month""",
            (tag,),
        )
        out[tag] = {"metrics": [tuple(r) for r in metrics], "battles": [tuple(r) for r in battles]}
    return out


def new_calendar_seed(new, expected_keys):
    missing = [
        k for k in expected_keys
        if _one(new, "SELECT COUNT(*) FROM recognition_ledger WHERE recognition_key = ?", (k,)) == 0
    ]
    return {"missing": missing}


# ---------------------------------------------------------------- driver

def run_baseline(old_path: str) -> int:
    old = _connect(old_path)
    checks = {
        "identity": old_identity,
        "links": old_links,
        "tenure": old_tenure,
        "awards": old_awards,
        "war_history": old_war_history,
        "rollups": old_rollups,
        "calendar_seed": old_calendar_seed,
    }
    failures = 0
    for name, fn in checks.items():
        try:
            result = fn(old)
            summary = _summarize(result)
            print(f"[baseline] {name:14s} OK   {summary}")
        except Exception as exc:  # a query that can't run on the old schema is a Phase-0 failure
            failures += 1
            print(f"[baseline] {name:14s} FAIL {exc}")
    if failures:
        print(f"\n{failures} baseline check(s) failed — fix before the cut.")
    else:
        print("\nAll baseline queries ran clean on the old schema.")
    return 1 if failures else 0


def _summarize(result):
    parts = []
    for key, val in result.items():
        if isinstance(val, list):
            parts.append(f"{key}={len(val)}")
        elif isinstance(val, dict):
            parts.append(f"{key}={len(val)}")
        else:
            parts.append(f"{key}={val}")
    return " ".join(parts)


def run_verify(old_path: str, new_path: str) -> int:
    old, new = _connect(old_path), _connect(new_path)
    tags = _sample_tags(old)
    expected_calendar = old_calendar_seed(old)["keys"]
    pairs = [
        ("identity", old_identity(old), new_identity(new)),
        ("links", old_links(old), new_links(new)),
        ("tenure", old_tenure(old), new_tenure(new)),
        ("awards", old_awards(old), new_awards(new)),
        ("war_history", old_war_history(old), new_war_history(new)),
        ("rollups", old_rollups(old), new_rollups(new, tags)),
    ]
    failures = 0
    for name, expected, actual in pairs:
        if expected == actual:
            print(f"[verify] {name:14s} PASS {_summarize(expected)}")
        else:
            failures += 1
            print(f"[verify] {name:14s} FAIL")
            print(f"         expected: {_summarize(expected)}")
            print(f"         actual:   {_summarize(actual)}")
    seed = new_calendar_seed(new, expected_calendar)
    if seed["missing"]:
        failures += 1
        print(f"[verify] calendar_seed  FAIL missing={seed['missing']}")
    else:
        print(f"[verify] calendar_seed  PASS keys={len(expected_calendar)}")
    print(f"\n{'ALL PARITY CHECKS PASS' if not failures else f'{failures} check(s) FAILED'}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    base = sub.add_parser("baseline", help="prove the old-side queries on the current DB")
    base.add_argument("--old", default="elixir-v5.db")
    ver = sub.add_parser("verify", help="compare archive vs new DB")
    ver.add_argument("--old", required=True)
    ver.add_argument("--new", required=True)
    args = parser.parse_args()
    if args.mode == "baseline":
        return run_baseline(args.old)
    return run_verify(args.old, args.new)


if __name__ == "__main__":
    sys.exit(main())
