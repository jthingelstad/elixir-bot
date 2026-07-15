"""Time-travel simulator — a synthetic war week through the REAL tick
(testing lever 2).

Drives the production engine.tick.run_tick path — poll planning, heat, the
anchored war clock, emission, projection, and management — against a fresh
scratch DB with a fake CR API and a frozen, fast-forwarded clock. Proactive
recognition/delivery is deliberately disabled, matching production: the
awareness brain is the sole posting owner. Time is fully synthetic: N days
pass in seconds, every timestamp derives from --start, nothing touches the
network, Discord, the LLM, or the live DB.

The world it simulates (deterministic; no randomness):
  - one river-race section with the ratified rhythm — 3 training days then
    4 battle days — with the daily reset at a SKEWED hour (default 09:37Z,
    the carried drift learning: CR's reset is never clean 10:00Z)
  - a member joining on day 2 and one leaving on day 5
  - a player crossing a collection_level_milestone (x100 grain) on day 3
  - two players fighting war battles on battle days (battlelog + race
    participants stay consistent)
  - donations accruing across the week

What it gates:
  - zero step errors across every tick (the _guard counters)
  - war_day_opened fires exactly once per battle day, labelled correctly
    ("battle day N of 4" — the raw-index leak class), observed at the first
    tick after the skewed boundary
  - membership events fire exactly once each and the awareness read sees the
    hard-post event stream
  - training-day race polling is hourly, battle-day polling every tick
  - war_participation accrues fame for the fighters and nobody else
  - the production path raises no legacy recognition claims, the retired
    delivery queue is absent, and global DB invariants hold

Usage:
    ./venv/bin/python scripts/simulate.py                 # 7 days, 30-min ticks
    ./venv/bin/python scripts/simulate.py --days 9 --reset 09:37 --keep
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

SEASON = 200
SECTION = 2  # periodIndex = SECTION*7 + day
HOME = "#J2RGCRVG"

# cast (stable synthetic tags — NOT real members, so no sim copy can ever be
# mistaken for a live person if a DB gets inspected side by side)
MEMBERS = [
    ("#SIM00001", "Anvil"),
    ("#SIM00002", "Bramble"),  # war fighter
    ("#SIM00003", "Cinder"),  # war fighter + leveler (44 -> 45 on day 3)
    ("#SIM00004", "Dune"),  # donor
    ("#SIM00005", "Ember"),  # leaves on day 5
]
JOINER = ("#SIM00006", "Flint")  # joins on day 2
FIGHTERS = {"#SIM00002", "#SIM00003"}
LEVELER = "#SIM00003"
LEAVER = "#SIM00005"


class SimWorld:
    """Deterministic state machine serving CR-shaped payloads for a sim time."""

    def __init__(self, start: datetime, reset_hh: int, reset_mm: int):
        from tests.conftest import load_cr_fixture

        self.player_fixture = load_cr_fixture("player_plain")
        self.battle_fixture = load_cr_fixture("battlelog")[0]
        self.now = start
        # anchor period 0 (training day 1) to the boundary BEFORE start, so
        # the sim begins mid-period like the live engine always does
        self.anchor0 = start.replace(
            hour=reset_hh, minute=reset_mm, second=0, microsecond=0
        ) - timedelta(days=1)
        self.race_calls_by_day: dict[int, int] = {}

    # --- world clock -----------------------------------------------------
    def day_index(self) -> int:
        """Periods elapsed since anchor0 (0-2 training, 3-6 battle, 7+ = next
        section's training — the sim crosses one section rollover)."""
        return max(0, int((self.now - self.anchor0) // timedelta(days=1)))

    def period_start(self, d: int) -> datetime:
        return self.anchor0 + timedelta(days=d)

    def roster_tags(self):
        tags = [t for t, _ in MEMBERS]
        if self.day_index() >= 2:
            tags.append(JOINER[0])
        if self.day_index() >= 5 and LEAVER in tags:
            tags.remove(LEAVER)
        return tags

    def _name(self, tag):
        return dict(MEMBERS + [JOINER]).get(tag, "?")

    def donations(self, tag):
        # Dune donates 60/day, everyone else 8/day — deterministic ramp
        rate = 60 if tag == "#SIM00004" else 8
        return rate * (self.day_index() + 1)

    def exp_level(self, tag):
        # expLevel is deprecated (dead at the CR API); kept only to shape the
        # legacy payload field. Progression celebration rides collection_level.
        return 44 if tag == LEVELER else 40

    def collection_level(self, tag):
        # LEVELER crosses a x100 Collection Level boundary (1699 -> 1705) mid-
        # day-3, firing collection_level_milestone (the CR 2026 progression
        # signal that replaced level_up). Everyone else holds steady.
        if tag == LEVELER and self.now >= self.period_start(3) + timedelta(hours=6):
            return 1705
        return 1699 if tag == LEVELER else 1500

    def _decks_by(self, tag, d) -> int:
        """War decks a fighter has played in period d, as of sim-now.
        Battles land at period_start + 3h + k*45min (~12:40-14:55Z)."""
        if tag not in FIGHTERS or not 3 <= d <= 6:
            return 0
        return sum(
            1
            for k in range(4)
            if self.period_start(d) + timedelta(hours=3, minutes=45 * k) <= self.now
        )

    def war_decks_today(self, tag):
        d = self.day_index()
        return self._decks_by(tag, d) if d <= 6 else 0

    def fame(self, tag):
        if self.day_index() >= 7:
            return 0  # new section — race fame resets
        return 225 * sum(self._decks_by(tag, p) for p in range(3, 7))

    # --- CR-shaped payloads (the fake cr_api) ------------------------------
    def get_clan(self):
        return {
            "tag": HOME,
            "name": "SIM KINGS",
            "clanScore": 620,
            "clanWarTrophies": 2200,
            "memberList": [
                {
                    "tag": t,
                    "name": self._name(t),
                    "role": "member",
                    "expLevel": self.exp_level(t),
                    "trophies": 5000,
                    "donations": self.donations(t),
                    "donationsReceived": 40,
                    "clanRank": i + 1,
                    "previousClanRank": i + 1,
                    "lastSeen": self.now.strftime("%Y%m%dT%H%M%S.000Z"),
                }
                for i, t in enumerate(self.roster_tags())
            ],
        }

    def get_current_war(self):
        d = self.day_index()
        self.race_calls_by_day[d] = self.race_calls_by_day.get(d, 0) + 1
        period_type = "training" if (d % 7) < 3 else "warDay"
        section = SECTION + d // 7
        return {
            "state": "full",
            "sectionIndex": section,
            "periodIndex": section * 7 + (d % 7),
            "periodType": period_type,
            "clan": {
                "tag": HOME,
                "name": "SIM KINGS",
                "fame": sum(map(self.fame, FIGHTERS)),
                "participants": [
                    {
                        "tag": t,
                        "name": self._name(t),
                        "fame": self.fame(t),
                        "repairPoints": 0,
                        "boatAttacks": 0,
                        "decksUsed": self.fame(t) // 225,
                        "decksUsedToday": self.war_decks_today(t),
                    }
                    for t in self.roster_tags()
                ],
            },
            "clans": [
                {
                    "tag": HOME,
                    "name": "SIM KINGS",
                    "fame": sum(map(self.fame, FIGHTERS)),
                    "periodPoints": 0,
                    "clanScore": 620,
                },
                {
                    "tag": "#SIMRIVAL",
                    "name": "SIM RIVAL",
                    "fame": 500 * max(0, d - 2),
                    "periodPoints": 0,
                    "clanScore": 610,
                },
            ],
        }

    def get_player(self, tag):
        p = dict(self.player_fixture)
        p.update(
            {
                "tag": tag,
                "name": self._name(tag),
                "expLevel": self.exp_level(tag),
                "trophies": 5000 + 10 * self.day_index(),
                "bestTrophies": 5400,
                "wins": 900 + self.day_index(),
                "donations": self.donations(tag),
            }
        )
        # Override the CollectionLevel badge progress so LEVELER's day-3 crossing
        # drives collection_level_milestone (the cards aspect reads this badge).
        p["badges"] = [
            {**b, "progress": self.collection_level(tag)}
            if b.get("name") == "CollectionLevel"
            else b
            for b in (p.get("badges") or [])
        ]
        return p

    def get_player_battle_log(self, tag):
        if tag not in FIGHTERS:
            return []
        battles = []
        for d in range(3, 7):
            for deck in range(self._decks_by(tag, d)):
                bt = self.period_start(d) + timedelta(hours=3, minutes=45 * deck)
                b = json.loads(json.dumps(self.battle_fixture))
                b["type"] = "riverRacePvP"
                b["battleTime"] = bt.strftime("%Y%m%dT%H%M%S.000Z")
                b.setdefault("gameMode", {})["name"] = "CW_Battle_1v1"
                b["team"][0]["tag"] = tag
                b["team"][0]["name"] = self._name(tag)
                b["team"][0]["crowns"] = 2
                b["opponent"][0]["crowns"] = 1
                battles.append(b)
        return list(reversed(battles))  # CR returns newest first


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument(
        "--start",
        default="2026-08-03T09:00:00Z",
        help="sim start (UTC); default begins just before a reset boundary",
    )
    ap.add_argument(
        "--reset",
        default="09:37",
        help="skewed daily reset hour (HH:MM UTC) — the drift learning",
    )
    ap.add_argument("--tick-minutes", type=int, default=30)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    reset_hh, reset_mm = (int(x) for x in args.reset.split(":"))

    scratch_dir = tempfile.mkdtemp(prefix="elixir-sim-")
    db_path = os.path.join(scratch_dir, "sim.db")
    os.environ["ELIXIR_DB_PATH"] = db_path  # storage.* facades in step 5

    from scripts.migrate_v51.schema_v51 import build

    build(db_path, os.path.join(_REPO, "elixir-v5-archive-2026H2.db"))

    import engine.tick as tick_mod
    from engine.db import connect

    conn = connect(db_path)
    # season inference needs one logged (season, section) waypoint
    conn.execute(
        "INSERT INTO war_seasons (season_id, started_at) VALUES (?, ?)",
        (SEASON, "2026-07-20T09:37:00Z"),
    )
    conn.execute(
        "INSERT INTO war_weeks (season_id, section_index, period_type, created_date) "
        "VALUES (?, ?, 'warDay', ?)",
        (SEASON, SECTION - 1, "2026-07-27"),
    )
    conn.commit()

    world = SimWorld(start, reset_hh, reset_mm)
    # freeze the Chicago day to sim time (calendar emitter, rollups)
    tick_mod.chicago_today = lambda: (world.now - timedelta(hours=5)).strftime(
        "%Y-%m-%d"
    )

    ticks = args.days * 24 * 60 // args.tick_minutes
    print(
        f"simulating {args.days} days = {ticks} ticks of {args.tick_minutes} min "
        f"from {args.start} (reset {args.reset}Z) into {db_path}"
    )
    errors = []
    for i in range(ticks):
        world.now = start + timedelta(minutes=args.tick_minutes * i)
        counters = tick_mod.run_tick(
            conn,
            world.now,
            api=world,
        )
        errors.extend(
            (world.now.strftime("%m-%dT%H:%M"), k, v)
            for k, v in counters.items()
            if k.endswith("_error")
        )
    end = world.now

    # --- gates -------------------------------------------------------------
    g: dict[str, bool] = {}
    g["zero step errors"] = not errors
    for e in errors[:10]:
        print("  step error:", e)

    wd = conn.execute(
        "SELECT dedup_key, observed_at, payload_json FROM war_events "
        "WHERE event_type='war_day_opened' ORDER BY observed_at"
    ).fetchall()
    expect_days = min(args.days, 4)  # battle days observed in a 7-day week sim
    g[f"war_day_opened x{expect_days}, once per battle day"] = len(
        wd
    ) == expect_days and len({r[0] for r in wd}) == len(wd)
    labels = [json.loads(r[2]).get("war_day_human") for r in wd]
    g["war-day labels are 1-based humans"] = labels == [
        f"battle day {i + 1} of 4" for i in range(len(wd))
    ]
    # observed at the first tick after the skewed boundary
    boundary_ok = True
    for r in wd:
        obs = datetime.fromisoformat(str(r[1]).replace("Z", "+00:00"))
        boundary = obs.replace(hour=reset_hh, minute=reset_mm, second=0)
        lag = (obs - boundary).total_seconds() / 60
        boundary_ok &= 0 <= lag <= args.tick_minutes
    g[f"war_day_opened within {args.tick_minutes}min of the {args.reset}Z reset"] = (
        boundary_ok
    )

    joins = conn.execute(
        "SELECT COUNT(*) FROM clan_events WHERE event_type='member_joined' AND subject_tag=?",
        (JOINER[0],),
    ).fetchone()[0]
    leaves = conn.execute(
        "SELECT COUNT(*) FROM clan_events WHERE event_type='member_left' AND subject_tag=?",
        (LEAVER,),
    ).fetchone()[0]
    g["one member_joined (day-2 joiner)"] = joins == 1
    g["one member_left (day-5 leaver)"] = leaves == 1
    lev = conn.execute(
        "SELECT COUNT(*) FROM player_events WHERE event_type='collection_level_milestone' AND player_tag=?",
        (LEVELER,),
    ).fetchone()[0]
    g["one collection_level_milestone (1700 crossing)"] = lev == 1

    fam = dict(
        conn.execute(
            "SELECT player_tag, SUM(fame) FROM war_participation WHERE season_id=? "
            "GROUP BY player_tag HAVING SUM(fame) > 0",
            (SEASON,),
        ).fetchall()
    )
    g["war fame accrued by fighters only"] = set(fam) == FIGHTERS and all(
        v > 0 for v in fam.values()
    )

    battles = conn.execute("SELECT COUNT(*) FROM battle_events").fetchone()[0]
    g["war battles mirrored"] = battles > 0

    if args.days >= 5:
        by_day = world.race_calls_by_day
        # full training periods 1-2 vs full battle periods 3+ (period 0 is
        # the 37-minute stub before the first boundary; the last period the
        # sim touches is partial too — skip both)
        last_full = world.day_index() - 1
        training = [by_day.get(d, 0) for d in (1, 2)]
        war = [by_day.get(d, 0) for d in range(3, min(last_full, 6) + 1)]
        # training: hourly (~24/day with 30-min ticks); war days: every tick
        g["race polls hourly in training, every tick in war"] = max(
            training, default=0
        ) < min(war, default=9999)

    if world.day_index() >= 7:
        wf = conn.execute(
            "SELECT COUNT(*) FROM war_events WHERE event_type='week_finished' "
            "AND dedup_key=?",
            (f"week_finished:{SEASON}:{SECTION}",),
        ).fetchone()[0]
        g["week_finished emitted at section rollover"] = wf == 1

    n_ledger = conn.execute("SELECT COUNT(*) FROM recognition_ledger").fetchone()[0]
    g["zero legacy recognition claims"] = n_ledger == 0
    g["retired delivery queue absent"] = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='communication_intents'"
        ).fetchone()
        is None
    )

    # The simulator stops at the LLM/network boundary, but it must prove the
    # awareness owner can see the hard stream events the engine produced.
    from runtime.awareness.read import build_read

    awareness_read = build_read(conn=conn)
    hard_types = {s.get("event_type") for s in awareness_read["hard_post_signals"]}
    g["awareness read sees hard-post stream events"] = bool(
        hard_types & {"member_joined", "week_finished"}
    )

    try:
        from tests.conftest import assert_db_invariants

        assert_db_invariants(conn, label="simulation")
        g["global DB invariants"] = True
    except AssertionError as exc:
        print(exc)
        g["global DB invariants"] = False

    print(
        f"\nsim ran {args.start} -> {end.strftime('%Y-%m-%dT%H:%M:%SZ')}; "
        f"{n_ledger} legacy claims, {battles} battles"
    )
    print("\n=== GATES ===")
    ok = True
    for k, v in g.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        ok = ok and v
    if args.keep or not ok:
        print(f"\nsim DB kept at {db_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
