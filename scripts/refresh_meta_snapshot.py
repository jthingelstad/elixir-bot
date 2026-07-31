#!/usr/bin/env python3
"""Snapshot the current Clash Royale meta with Opus 5 + web search (Deck Intelligence).

The clan cannot answer "what decks should I consider?". 46 members are a thin slice of
the meta, and the local numbers are skill-confounded: player skill spans 36.4%-70.2%
(as wide as the deck spread) and only 2 shared-deck observations exist clan-wide, so no
deck's record transfers between members. A member in a rut specifically needs decks
nobody here plays — exactly where local data is silent.

Design (the card-facts enricher pattern, reused because it worked):
  * **Rare and batched.** One call produces a durable asset; every member question then
    reads it for $0. Refresh on balance patches, not on demand.
  * **The model never picks for a member.** It reports what is strong right now, in card
    NAMES. Ownership, Evo/Hero form and card level are enforced afterwards in SQL, so a
    deck the member cannot build can never reach them.
  * **Names are catalog-resolved, and failures are kept.** A hallucinated or renamed card
    cannot enter a recommendation; it lands in ``unresolved_json`` where it is visible
    as data rather than silently dropping a deck to 7 cards.

Usage:
    python scripts/refresh_meta_snapshot.py --dry-run     # print, don't write
    python scripts/refresh_meta_snapshot.py               # write a new snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "claude-opus-5"
WANT_DECKS = 12

PROMPT = """Research the CURRENT competitive Clash Royale meta using web search and \
report the strongest ladder/Path-of-Legends decks being played right now.

Use web search first. Prefer sources updated within the last few weeks (RoyaleAPI \
meta/decks pages, StatsRoyale, current tier lists, recent balance-change coverage). \
Today is {today}. Note the most recent balance update you can confirm.

Return EXACTLY {want} decks spanning different archetypes -- do not return {want} \
variations of one deck. For each deck give:

  name           short recognizable name, e.g. "Hog 2.6 Cycle"
  archetype      e.g. "Hog Cycle", "Log Bait", "Golem Beatdown"
  family         one of: beatdown, control, cycle, bait, bridge spam, siege
  tier           S, A or B as reported by your sources
  win_condition  the primary win condition card
  cards          EXACTLY 8 card names, spelled as the Clash Royale API spells them
                 (e.g. "Mini P.E.K.K.A", "P.E.K.K.A", "The Log", "Barbarian Barrel")
  evolutions     card names in this deck played as EVOLUTIONS (subset of cards, may be [])
  note           one sentence on why it is strong right now
  source_url     the page you took it from

Rules:
  * Card names must be real Clash Royale cards, spelled exactly. Do not invent cards.
  * Exactly 8 cards per deck, no duplicates within a deck.
  * If sources disagree, prefer the more recent one and say so in `note`.

Respond with ONLY a JSON object: {{"balance_update": "<what you confirmed>", \
"decks": [ ... ]}} -- no prose, no markdown fence."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in response: {text[:300]}")
    return json.loads(text[start : end + 1])


def fetch(client) -> dict:
    """One Opus call with web search. Handles `pause_turn` by continuing the turn."""
    messages = [
        {
            "role": "user",
            "content": PROMPT.format(
                today=datetime.now(timezone.utc).strftime("%Y-%m-%d"), want=WANT_DECKS
            ),
        }
    ]
    for _ in range(6):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "xhigh"},  # infrequent, durable asset — favor quality
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _extract_json(text)
    raise RuntimeError("web search did not converge after 6 continuations")


def resolve(conn, decks: list[dict]) -> list[dict]:
    """Map card names to catalog ids, form-aware. Unknown names are RETAINED (not
    silently dropped) so a rename or hallucination is visible in the stored row."""
    catalog = {
        r[1].lower(): (r[0], r[2])
        for r in conn.execute("SELECT card_id, name, max_evolution_level FROM card_catalog")
    }
    out = []
    for d in decks:
        names = d.get("cards") or []
        evos = {str(e).lower() for e in (d.get("evolutions") or [])}
        pairs, unresolved = [], []
        for n in names:
            hit = catalog.get(str(n).strip().lower())
            if not hit:
                unresolved.append(n)
                continue
            cid, maxevo = hit
            form = 1 if (str(n).strip().lower() in evos and (maxevo or 0) >= 1) else 0
            pairs.append([cid, form])
        d = dict(d)
        d["_pairs"] = pairs
        d["_unresolved"] = unresolved
        out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("ELIXIR_DB_PATH"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("CLAUDE_API_KEY not set", file=sys.stderr)
        return 2
    client = anthropic.Anthropic(api_key=key)

    print(f"asking {MODEL} for the current meta (web search enabled)...")
    payload = fetch(client)
    decks = payload.get("decks") or []
    print(f"balance update reported: {payload.get('balance_update')!r}")
    print(f"decks returned: {len(decks)}")

    if not args.db:
        print("no --db / ELIXIR_DB_PATH", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    try:
        decks = resolve(conn, decks)
        stamp = _now()
        good = 0
        for d in decks:
            ok = len(d["_pairs"]) == 8
            flag = "" if ok else f"  !! {len(d['_pairs'])}/8 cards, unresolved={d['_unresolved']}"
            print(
                f"  [{d.get('tier', '?')}] {d.get('name', '?')[:34]:34} {d.get('family', '?'):12}{flag}"
            )
            if not ok:
                continue
            good += 1
            if args.dry_run:
                continue
            conn.execute(
                "INSERT INTO meta_decks (name, archetype, family, tier, cards_json, "
                "unresolved_json, win_condition, note, source_url, model, snapshot_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    d.get("name"),
                    d.get("archetype"),
                    d.get("family"),
                    d.get("tier"),
                    json.dumps(d["_pairs"]),
                    json.dumps(d["_unresolved"]) if d["_unresolved"] else None,
                    d.get("win_condition"),
                    d.get("note"),
                    d.get("source_url"),
                    MODEL,
                    stamp,
                ),
            )
        if args.dry_run:
            print(f"\ndry run — {good}/{len(decks)} decks resolved cleanly, nothing written")
        else:
            conn.commit()
            print(f"\nwrote {good} decks at snapshot {stamp}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
