"""Seed the Editor's founding rubric entries (editor.md §3) + create the
retired inline-verdict mechanism. Idempotent — every entry dedupes on its
source_event_key; safe to re-run.

Post texts are verbatim from Discord (message ids in provenance), fetched
2026-07-04; the Andy welcome is pre-cut (Gen-A, 2026-06-28), recovered from
#clan-events message 1520822113349402788.

Usage: ./venv/bin/python scripts/seed_editorial_rubric.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FOUNDING_ENTRIES = [
    dict(
        key="editorial_seed:sikander-flat-welcome",
        kind_tag="anti-pattern",
        title="Anti-pattern: template-grade welcome (sikander)",
        body=(
            "Welcome post that shows zero looking-at-the-member — it could have "
            "been a fill-in-the-blank template. Jamie: 'Elixir should sound like "
            "a real agent. That missed the mark.'\n\n"
            "POST (#clan-events, member_joined, 2026-07-04, msg 1522914139662585956):\n"
            "Welcome to POAP KINGS, sikander sidhu. \U0001f451\n\n"
            "event: member_joined lane: clan-events reception welcome"
        ),
    ),
    dict(
        key="editorial_seed:momentum-is-real-stock-phrase",
        kind_tag="anti-pattern",
        title="Anti-pattern: stock phrase recycled across posts ('momentum is real')",
        body=(
            "The same canned line appeared in three arena posts within minutes — "
            "phrasing must vary; a stock line repeated across posts reads as a bot.\n\n"
            "POSTS (#player-highlights, celebrate:arena_up, 2026-07-04, msgs "
            "1522808762220544010 / 1522808929187528954 / 1522808984502009907):\n"
            "1. **Andy** just climbed into **PANCAKES!** :trophy: Clean 3-crown "
            "sweep on the way up — momentum is real.\n"
            "2. **gtr0925** just climbed into **Summit of Heroes**. :trophy: "
            "Fresh off a 2v2 win too — momentum is real.\n"
            "3. **JaxikoLane** just climbed into **Summit of Heroes**. :trophy: "
            "Fresh win on the board — momentum is real.\n\n"
            "event: arena_up trophy_push lane: member-highlights freshness"
        ),
    ),
    dict(
        key="editorial_seed:64-members-unverified-count",
        kind_tag="anti-pattern",
        title="Anti-pattern: unverified number stated as fact ('64 members')",
        body=(
            "The weekly invite post claimed 'We're at 64 members and full roster' "
            "while the real roster was 47 — the count came from stale membership "
            "rows, not a live check. Numbers in copy must trace to verified "
            "current data.\n\n"
            "POST (#leader-actions, invite relay, 2026-07-04, msg 1522995431267631226):\n"
            "**Weekly invite check:** We're at 64 members and full roster, but not "
            "everyone's in Discord yet. [...]\n\n"
            "event: invite_relay lane: arena-relay grounding"
        ),
    ),
    dict(
        key="editorial_seed:eddieplayz-invented-specific",
        kind_tag="anti-pattern",
        title="Anti-pattern: invented specific ('a deep card collection')",
        body=(
            "The welcome invented 'a deep card collection' — nothing in the "
            "facts said anything about cards (facts had: trophies 10396, role "
            "member, exp_level 0). Plausible-sounding specifics NOT in the facts "
            "are the founding failure class of the Editor.\n\n"
            "POST (#clan-events, member_joined, 2026-07-04, msg 1522997528864227428):\n"
            "Welcome to POAP KINGS, **EddiePlayz**. \U0001f451 Over 10k trophies "
            "and a deep card collection — you're bringing real strength to the "
            "clan. Looking forward to seeing you battle with us.\n\n"
            "event: member_joined lane: clan-events grounding"
        ),
    ),
    dict(
        key="editorial_seed:eddieplayz-structure-warmth",
        kind_tag="exemplar",
        title="Exemplar: welcome structure and warmth (EddiePlayz — minus the invention)",
        body=(
            "Same EddiePlayz welcome, kept as the STRUCTURE to emulate: name, one "
            "concrete first impression, a warm forward-looking close. 'Over 10k "
            "trophies' was grounded (facts: 10396) — that half is the model; only "
            "the invented card-collection clause fails.\n\n"
            "POST (#clan-events, member_joined, 2026-07-04, msg 1522997528864227428):\n"
            "Welcome to POAP KINGS, **EddiePlayz**. \U0001f451 Over 10k trophies "
            "[...] — you're bringing real strength to the clan. Looking forward "
            "to seeing you battle with us.\n\n"
            "event: member_joined lane: clan-events substance"
        ),
    ),
    dict(
        key="editorial_seed:andy-grounded-first-impression",
        kind_tag="exemplar",
        title="Exemplar: grounded concrete first impression (Andy welcome)",
        body=(
            "A welcome that proves someone actually looked: one real, verifiable "
            "number from the member's recent play, delivered in-voice. This is "
            "the substance bar for welcomes.\n\n"
            "POST (#clan-events, member_joined, pre-cut Gen-A 2026-06-28, msg "
            "1520822113349402788):\n"
            "Welcome to POAP KINGS, Andy. \U0001f451\n"
            "Coming in hot — 620 trophies in the last two days. We like to see it.\n\n"
            "event: member_joined lane: clan-events substance grounding"
        ),
    ),
]


def seed(conn) -> dict:
    from engine import editor

    editor.ensure_editor_schema(conn)
    counters = {"added": 0, "skipped": 0}
    for entry in FOUNDING_ENTRIES:
        mid = editor._add_editorial_memory(
            conn,
            title=entry["title"],
            body=entry["body"],
            kind_tag=entry["kind_tag"],
            event_key=entry["key"],
            confidence=0.9,
            created_by="editorial-seed",
            extra_tags=("founding",),
            source_type="leader_note",  # Jamie-ratified founding entries
        )
        counters["added" if mid else "skipped"] += 1
    conn.commit()
    return counters


def main():
    from engine import db as engine_db

    conn = engine_db.connect()
    try:
        counters = seed(conn)
        total = conn.execute(
            "SELECT COUNT(*) FROM memory_tags WHERE tag = 'editorial'"
        ).fetchone()[0]
        print(f"seeded: {counters} (editorial-tagged memories now: {total})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
