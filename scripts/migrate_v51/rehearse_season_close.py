"""Season-close rehearsal against a scratchpad copy — drives emit_race exactly
as the tick would, through the Sunday sequence: final colosseum day, then the
rollover observation (§16.1: the birth of s134 IS the death of s133)."""
import json
import sys
import os
sys.path.insert(0, "/Users/otto/Projects/elixir-bot")
os.environ["ELIXIR_DB_PATH"] = sys.argv[1]

from engine.db import connect
from engine.clock import infer_season_id
from engine.emitters import emit
from engine.emitters.war import project_race_aspect
from engine import recognition
from engine.recognition.compose import render_intent

DB = sys.argv[1]
HOME = "#J2RGCRVG"
conn = connect(DB)

base = json.loads(conn.execute(
    "SELECT payload_json FROM state_baselines WHERE entity_kind='riverrace'").fetchone()[0])

def cr_shaped(section, period, ptype, fame, participants):
    return {
        "sectionIndex": section, "periodIndex": period, "periodType": ptype,
        "clan": {"tag": HOME, "fame": fame, "participants": participants},
        "clans": [{"tag": HOME, "fame": fame, "clanScore": 620},
                  {"tag": "#GRPCVYGP", "fame": 0, "clanScore": 617},
                  {"tag": "#J29PVQL0", "fame": 120, "clanScore": 618}],
    }

# participants reconstructed from the stored projection (CR-shaped-ish)
participants = [
    {"tag": t, "name": (p or {}).get("name"), "fame": (p or {}).get("fame"),
     "repairPoints": (p or {}).get("repair_points"),
     "boatAttacks": (p or {}).get("boat_attacks"),
     "decksUsed": (p or {}).get("decks_used"),
     "decksUsedToday": (p or {}).get("decks_used_today")}
    for t, p in (base.get("participants") or {}).items()
]

def drive(payload, at):
    sid = infer_season_id(conn, payload)
    aspect = project_race_aspect(payload, sid)
    n = emit(conn, "riverrace", HOME, "race", aspect, at)
    conn.commit()
    return sid, n

# A) final colosseum battle day (day 4 of 4: period 34), fame progresses
sid_a, n_a = drive(cr_shaped(4, 34, "colosseum", 30000, participants), "2026-07-05T10:05:00Z")
print(f"A: final-day emit — inferred season {sid_a}, events {n_a}")

# B) THE ROLLOVER — new season observed: section 0, training, period 0
sid_b, n_b = drive(cr_shaped(0, 0, "training", 0, participants), "2026-07-06T10:05:00Z")
print(f"B: rollover emit — inferred season {sid_b} (must be 134), events {n_b}")

# C) idempotence: same rollover payload again
sid_c, n_c = drive(cr_shaped(0, 0, "training", 0, participants), "2026-07-06T10:15:00Z")
awards_before = conn.execute("SELECT COUNT(*) FROM awards WHERE season_id=133").fetchone()[0]
sid_c2, n_c2 = drive(cr_shaped(0, 0, "training", 0, participants), "2026-07-06T10:25:00Z")
awards_after = conn.execute("SELECT COUNT(*) FROM awards WHERE season_id=133").fetchone()[0]
print(f"C: idempotence — re-emits produced {n_c + n_c2} events, awards {awards_before}->{awards_after}")

# D) war recognizer over the new events (rolled clock: training, s134)
clock = {"phase": "training", "season_id": 134, "section_index": 0,
         "is_colosseum_week": False, "pace_status": "training",
         "hours_left_in_period": 20.0, "race_finished": False, "day_index": 0}
rec = recognition.run_recognizers(conn, clock, "2026-07-06T10:06:00Z")
conn.commit()
print("D: recognizer counters:", rec)

print("\n=== GATES ===")
g = {}
rows = {r[0] for r in conn.execute("SELECT dedup_key FROM war_events")}
g["week_finished:133:4"] = "week_finished:133:4" in rows
g["season_closed:133"] = "season_closed:133" in rows
g["season_started:134"] = "season_started:134" in rows
s = conn.execute("SELECT ended_at, final_rank, weeks, war_champ_tag, free_pass_tag FROM war_seasons WHERE season_id=133").fetchone()
top = conn.execute("SELECT player_tag, SUM(fame) f FROM war_participation WHERE season_id=133 GROUP BY 1 ORDER BY f DESC LIMIT 2").fetchall()
g["s133 finalized"] = bool(s and s[0])
g["champ == top fame"] = bool(s and s[3] == top[0][0])
g["rotation vs s132 (#VQCYJQY0P)"] = bool(s and (s[4] == s[3]) == (s[3] != "#VQCYJQY0P"))
aw = {r[0]: (r[1], r[2]) for r in conn.execute(
    "SELECT award_type, COUNT(*), GROUP_CONCAT(player_tag || ':r' || rank) FROM awards WHERE season_id=133 GROUP BY 1")}
g["war_champ podium x3"] = aw.get("war_champ", (0,))[0] == 3
g["free_pass x1"] = aw.get("free_pass", (0,))[0] == 1
g["donation_champ present"] = "donation_champ" in aw
g["war_participant == fame>0"] = aw.get("war_participant", (0,))[0] == conn.execute(
    "SELECT COUNT(*) FROM (SELECT player_tag FROM war_participation WHERE season_id=133 GROUP BY 1 HAVING SUM(fame)>0)").fetchone()[0]
g["iron_king absent"] = "iron_king" not in aw
led = conn.execute("SELECT COUNT(*), COUNT(DISTINCT recognition_key) FROM recognition_ledger").fetchone()
g["ledger no dupes"] = led[0] == led[1]
intents = conn.execute(
    "SELECT intent_id, intent_type, lane, payload_json FROM communication_intents WHERE intent_type IN ('war:season_closed','clan:season_awards') OR intent_type LIKE '%season%'").fetchall()
g["season_closed intent"] = any("season_closed" in (i[1] or "") for i in intents)
g["season_awards intent -> clan-events"] = any(i[1] == "clan:season_awards" and i[2] == "clan-events" for i in intents)
for k, v in g.items():
    print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print("\nrows detail:", json.dumps(aw, indent=1))
print("s133:", dict(zip(("ended_at","final_rank","weeks","champ","free_pass"), s)) if s else None)
print("standings top2:", [tuple(t) for t in top])

print("\n=== FALLBACK COPY ===")
for i in intents:
    row = conn.execute("SELECT * FROM communication_intents WHERE intent_id=?", (i[0],)).fetchone()
    print(f"[{i[1]} -> {i[2]}]")
    print(" ", render_intent(row))
conn.close()
