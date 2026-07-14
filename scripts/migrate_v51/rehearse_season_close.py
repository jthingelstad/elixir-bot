"""Current-data-relative season-close rehearsal.

Runs against a scratch database (normally the replay gate's copy), selects the
latest open season with real participation, closes it twice, and verifies the
awareness-era contract:

* the season and durable award rows finalize once;
* a ``season_closed`` stream event exists exactly once;
* no retired deterministic communication intent is enqueued;
* the awareness read can see the new season-close signal; and
* repeating the close creates no duplicate event or award rows.

Unlike the original migration-day script, this contains no fixed dates, season
IDs, player tags, or historical award expectations. It stays useful as the live
database advances.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: rehearse_season_close.py /path/to/scratch.db", file=sys.stderr)
        return 2

    db_path = os.path.abspath(sys.argv[1])
    os.environ["ELIXIR_DB_PATH"] = db_path

    from engine.db import connect
    from engine.emitters.war import close_season
    from runtime.awareness.read import build_read

    conn = connect(db_path)
    row = conn.execute(
        """SELECT s.season_id
             FROM war_seasons s
            WHERE s.ended_at IS NULL
              AND EXISTS (
                    SELECT 1 FROM war_participation wp
                     WHERE wp.season_id = s.season_id
              )
            ORDER BY s.season_id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        print("SKIP: no open season with participation to rehearse")
        conn.close()
        return 0

    season_id = int(row["season_id"])
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_key = f"season_closed:{season_id}"

    before_awards = conn.execute(
        "SELECT COUNT(*) FROM awards WHERE season_id = ?", (season_id,)
    ).fetchone()[0]
    before_intents = conn.execute("SELECT COUNT(*) FROM communication_intents").fetchone()[0]

    first_events = close_season(conn, season_id, {}, observed_at)
    conn.commit()
    after_first_awards = conn.execute(
        "SELECT COUNT(*) FROM awards WHERE season_id = ?", (season_id,)
    ).fetchone()[0]
    second_events = close_season(conn, season_id, {}, observed_at)
    conn.commit()
    after_second_awards = conn.execute(
        "SELECT COUNT(*) FROM awards WHERE season_id = ?", (season_id,)
    ).fetchone()[0]

    season = conn.execute(
        "SELECT ended_at, war_champ_tag, free_pass_tag FROM war_seasons WHERE season_id = ?",
        (season_id,),
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM war_events WHERE dedup_key = ?", (event_key,)
    ).fetchone()[0]
    intent_count = conn.execute("SELECT COUNT(*) FROM communication_intents").fetchone()[0]
    awards = dict(
        conn.execute(
            "SELECT award_type, COUNT(*) FROM awards WHERE season_id = ? GROUP BY award_type",
            (season_id,),
        ).fetchall()
    )

    read = build_read(conn=conn)
    surfaced = [
        signal
        for signals in (read.get("signals_by_lane") or {}).values()
        for signal in (signals or [])
        if isinstance(signal, dict)
        and signal.get("event_type") == "season_closed"
        and str(signal.get("subject_tag")) == str(season_id)
    ]
    surfaced.extend(
        signal
        for signal in (read.get("hard_post_signals") or [])
        if isinstance(signal, dict)
        and signal.get("event_type") == "season_closed"
        and str(signal.get("subject_tag")) == str(season_id)
    )

    gates = {
        "season finalized": bool(season and season["ended_at"]),
        "war champ recorded": bool(season and season["war_champ_tag"]),
        "free pass recorded": bool(season and season["free_pass_tag"]),
        "season_closed emitted once": first_events == 1 and second_events == 0 and event_count == 1,
        "war champ podium present": 1 <= awards.get("war_champ", 0) <= 3,
        "free pass exactly once": awards.get("free_pass", 0) == 1,
        "awards idempotent": after_first_awards >= before_awards
        and after_second_awards == after_first_awards,
        "no legacy intent enqueued": intent_count == before_intents,
        "awareness read sees season close": bool(surfaced),
    }

    print(f"season-close rehearsal: season {season_id} at {observed_at}")
    print(f"awards: {awards}")
    print("\n=== GATES ===")
    ok = True
    for label, passed in gates.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
