# Elixir v5.1 — Recognition Spec

> **Status:** ✅ Build-ready (`arena_up` 85/bypass ratified 2026-07-03, §9).
> **Owner:** Jamie · **Last worked:** 2026-07-03
>
> The notability layer: the deterministic scorer **ported** from
> `event_core/mind/communication.py` (constants reproduced verbatim, line-cited),
> the shared recognition ledger, stream→lane routing, and composition enrichment.
> Per §10: emission is code, notability is code, **voice is the subagent** — the
> LLM decides framing, never whether something scored high enough.
> Event names are the v5.1 names from `events.md` §6 (the C2 mapping).

## 1. The pipeline per tick

```
stream events (new since cursor)
  → per-stream recognizer builds candidates
  → deterministic scorer (this doc §2–§4): threshold / bypass / accrual / coalesce / cohort
  → ledger claim (§5): one real moment → one post, cross-stream
  → communication intent raised (schema.md §7.2), routed (§6)
  → stream subagent composes voice (§7) → delivery (runtime.md)
```

**Which moments the scorer governs — the boundary, stated once.** The
threshold/accrual/coalesce machinery (§2–§3) applies to **celebrate-class**
moments only: player events plus derived battle moments (everything routed with
the `celebrate` prefix). **Clan-social and war moments bypass the scorer
entirely** — they post via the direct path with per-type guards (§4), exactly as
today's `_CLAN_SOCIAL` types do (`communication.py:51–59`). Every moment of
either class still claims the ledger (§5); the scorer decides *worthiness*, the
ledger decides *uniqueness*, and only celebrate-class moments need the former.

A candidate that fails scoring is **dropped with a recorded reason** (today's
suppressed-intent pattern, `communication.py:473–500`): the engine can always
answer "why didn't you post X?".

## 2. The ported scorer — constants are law

Source: `event_core/mind/communication.py:70–105, 185–201`. These values are tuned
against real spam incidents (§17.2) and port **unchanged**. Keys are translated to
v5.1 event names per `events.md` §6; scores do not change.

```
HIGHLIGHT_THRESHOLD   = 80          # communication.py:71
ACCRUAL_WINDOW        = 14 days     # communication.py:70 (PLAYER_HIGHLIGHT_WINDOW)
```

**Base scores** (`_PLAYER_HIGHLIGHT_BASE_SCORES`, `communication.py:76–87`):

| v5.1 event (was) | Base score | Bypass? |
|---|---|---|
| `ultimate_champion_reached` | 120 | ✅ |
| `pol_global_rank_attained` (`path_of_legend_global_rank_attained`) | 110 | ✅ |
| `card_level_milestone` | 95 | ✅ |
| `career_wins_milestone` | 85 | ✅ |
| `collection_level_milestone` | 80 | ✅ |
| `level_up` (`player_level_up`) | 80 | ✅ |
| `badge_earned` | 55 | — |
| `pol_promotion` (`path_of_legend_promotion`) | 45 | — |
| `best_trophies_peak` | 40 | — |
| `ranked_pulse` (`ranked_activity_pulse`) | 30 | — |

Bypass set = `_PLAYER_HIGHLIGHT_BYPASS_TYPES` (`communication.py:89–96`): a bypass
event posts immediately regardless of accrued score.

**Dynamic scores** (`_score_detection`, `communication.py:185–201`):

| Event | Rule |
|---|---|
| `card_unlocked` | rarity `champion` → **90, bypass**; rarity `legendary` → **65**; any other rarity → 0 (not notable alone) |
| `trophy_push` | `min(45, 25 + max(0, delta − 100) // 50 × 5)` — delta-scaled, capped, never bypass |

**Milestone ladders** (the emitters' natural keys, `detectors.py`; carried so the
scores keep meaning):

| Event | Ladder | Source |
|---|---|---|
| `level_up` | every 5 levels (`_milestones(step=5)`) | `detectors.py:35` |
| `best_trophies_peak` | every 100 trophies | `:53` |
| `career_wins_milestone` | every 1,000 wins (`STEP = 1000`) | `:67` |
| `collection_level_milestone` | every 100 (`STEP = 100`) | `:297` |
| `card_level_milestone` | each level ≥ 16 (`MIN_LEVEL = 16`) | `:187` |
| `ultimate_champion_reached` | Path-of-Legend league 10 (`ULTIMATE_CHAMPION_LEAGUE = 10`) | `:91` |
| `trophy_push` | a run of ≥3 consecutive positive-trophy competitive battles totaling ≥100 (`MIN_BATTLES = 3`, `MIN_DELTA = 100`); keyed on the run's **first** battle (revised 2026-07-04 — see events.md §2) | `:334–346` |
| `ranked_pulse` | ≥12 PoL battles with ≥12 decided, ≥9 wins, ≥70% win rate in a trailing 7-day window (`WINDOW_DAYS = 7`, `MIN_BATTLES = 12`, `MIN_DECIDED = 12`, `MIN_WINS = 9`, `MIN_WIN_RATE = 0.70`) | `:400–405` |
| `hot_streak` | — **not computed in v5.1** (matches today: detector intentionally unregistered; contributes no accrual evidence) | events.md §2 |

**Same-tick priority sort** (`_CELEBRATE_PRIORITY`, `communication.py:98–111` —
reproduced verbatim, translated to v5.1 names, since Phase 4 deletes the source):

| v5.1 event | Priority |
|---|---|
| `ultimate_champion_reached` | 100 |
| `pol_global_rank_attained` | 95 |
| `card_level_milestone` | 80 |
| `card_unlocked` | 75 |
| `badge_earned` | 70 |
| `collection_level_milestone` | 65 |
| `career_wins_milestone` | 60 |
| `level_up` | 55 |
| `pol_promotion` | 50 |
| `best_trophies_peak` | 40 |
| `ranked_pulse` | 15 |
| `trophy_push` | 10 |
| `arena_up` | **90** (new — slots between rank-attained and card-milestone, consistent with its 85/bypass score) |

## 3. Accrual, bypass, coalescing — the anti-spam core

Ported from `_process_celebrate_candidates` (`communication.py:501–560`):

1. **Group** same-tick celebrate candidates per subject tag.
2. **Select** one per subject by priority sort (`_candidate_sort_key`,
   `communication.py:324–338`): celebrate-priority table
   (`_CELEBRATE_PRIORITY`, `:98–111`), then payload magnitude
   (milestone/level/peak/delta), then time, then arrival order. The rest of the
   group is **coalesced** — dropped with reason `coalesced:same_tick`, recorded
   against the selected moment. One post when several milestones land together.
3. **Accrue**: evidence = the subject's celebrate-type events in the trailing
   **14-day window**, cut off at the subject's **last posted highlight** (no
   double-counting already-celebrated evidence; `_recognition_evidence`,
   `communication.py:387–433`). Score = **sum** over evidence.
4. **Decide**: post if `selected is bypass` OR `sum ≥ 80`; otherwise drop all with
   reason `accruing` (the evidence keeps accruing toward a future post).
5. Every intent records its full scoring trace (`_recognition_summary`,
   `communication.py:452–471`): policy id, decision, score, threshold, evidence
   list. v5.1 keeps this — it is how prompt-failure review explains silence.

**Cohort waves** (from the retired `cohort_wave` detector, `detectors.py:829–880`):
if **≥3 members** (`MIN_MEMBERS = 3`) hit the same milestone type in the same
**America/Chicago day**, for wave types `badge_earned`, `card_level_milestone`,
`card_unlocked` (`WAVE_TYPES`, `:835`), the recognizer raises **one cohort moment**
(ledger key `cohort_wave:{event_type}:{chicago_day}`) naming the members, instead
of (not in addition to) later per-member accruals that day. Individual bypass
moments already posted stay posted — the wave covers the accruing remainder.

## 4. Per-stream recognizers

- **Player recognizer** — consumes `player_events`; the scorer above is its core.
- **Battle recognizer** — computes derived moments (`events.md` §2) from new
  `battle_events`: `arena_up` (trophy-boundary crossing on a win; **the moment**,
  §11), `trophy_push`, `ranked_pulse`. Scores: `trophy_push` dynamic (above);
  `ranked_pulse` 30; **`arena_up` is new — 85, bypass** (an arena-up is
  rarer and more meaningful than a level-up at 80; the one new constant —
  **ratified by Jamie, 2026-07-03**).
- **Clan recognizer** — consumes `clan_events`. Clan-social moments post without
  the 80-threshold (today `_CLAN_SOCIAL` types raise intents directly,
  `communication.py:51–59`): joins, leaves, role changes, birthdays,
  anniversaries, donation leader — **plus the new clan-entity types**
  `clan_score_milestone` and `clan_league_changed` (same direct path; they are
  rare clan-level facts, not member celebrations, so the accrual scorer has no
  meaning for them). Guards:
  - `member_left` → **kick-suppression** (C1): skip the public post if a `done`
    `kick_recommendation` exists for the tag within **14 days**
    (`_KICK_SUPPRESS_DAYS = 14`, `detectors.py:542`; query shape `:568–582`).
    The event still exists; only the post is suppressed.
  - `role_changed(direction=demoted)` → **no public post** (new type; demotions
    are leadership reality, not celebration). The event feeds history reads and
    the management projection only.
- **War recognizer** — consumes `war_events` (schema.md §5.3) + the war clock.
  War moments take the **direct path** (§1 boundary — no 80-threshold; a war
  event is already a once-per-week-scale fact) with the clock as their guard:
  `war_day_opened` / pace moments are **phase-gated** (§16.2): no boat-defense
  talk in Colosseum, urgency drops after `race_finished` (§16.4). `season_closed`
  composes the season recap + War Champ announcement (§16.3), and per Q2/C5 it
  distinguishes **honor from reward** when the rotation rule splits them
  (`war_champ_tag ≠ free_pass_tag` → the copy congratulates the champ AND names
  the free-pass recipient with the rotation rationale).

**Arena-up cross-stream correlation** (§11): the battle recognizer claims
`arena_up:{tag}:{arena_id}` from the deciding battle (exact timing); the player
recognizer, on `arena_changed`, attempts the **same key** — if the battle already
claimed it, the profile event merely confirms; if the deciding battle was missed
(poll gap, battlelog depth ceiling — observed mode 30), the profile claim wins and
posts with estimated timing. **Either claimant scores it as `arena_up` — 85,
bypass** (`arena_changed` has no score of its own; it exists only to reach this
key). One moment, one post, whichever stream saw it first.

## 5. The recognition ledger

Table in `schema.md` §7.1. Key formulas follow the emitters' dedup discipline:

| Moment | `recognition_key` |
|---|---|
| player/clan/war event moments | the event's `dedup_key` verbatim |
| derived battle moments | `events.md` §2 keys (`arena_up:{tag}:{arena_id}`, …) |
| cohort waves | `cohort_wave:{event_type}:{chicago_day}` |
| award grants (Q5) | `award:{award_type}:{season_id}:{player_tag}` |

Claim happens **before** intent-raising, inside the recognizer's transaction.
`INSERT OR IGNORE` + rowcount tells the second stream to back off (§10). The
ledger row stores the scoring trace summary (`score`, `event_refs_json`) and is
updated with `intent_id` when the intent is raised — claims without intents are
legitimate (suppressed moments still claim, so a re-scan can't resurrect them).

## 6. Routing — intent prefix → lane

Ported from `route_intent` + `_PREFIX_CHANNEL`
(`event_core/live/runtime.py:17–40`), with channel ids resolved from
`prompts/DISCORD.md` at runtime instead of hard-coded (the current file hard-codes
ids — carrying that would re-create config drift):

| Prefix | Lane (channel) | Notes |
|---|---|---|
| `celebrate` | `member-highlights` (`#player-highlights`) | player + battle moments |
| `clan` | `clan-events` (`#clan-events`) | clan-social + awards (Q5) |
| `cohort` | `clan-events` | cohort waves |
| `war` | `river-race` (`#river-race`) | war stream + season recap |
| `welcome` | `reception` (`#welcome`) | reception handoffs |
| `leadership` or scope=`leadership` | `arena-relay` (`#leader-actions`) | leader-action card pipeline |
| **unknown prefix** | `arena-relay` — **fail-closed** | carried: unknown never leaks public (`runtime.py:33–40`) |

**Reading this table:** the middle column's *lane key* is a legacy config key,
**not** the channel name — the actual Discord channel is the parenthesized
`#name`, resolved via `DISCORD.md`. The two footguns: `member-highlights`
resolves to `#player-highlights`, and the leadership lane's key is
`arena-relay` (a historical name from the screenshot-relay era) but resolves
to `#leader-actions`. Route by lane key; render by channel.

## 7. Composition — voice is the subagent, enrichment is code

Carried from `compose_copy` / `make_agent_poster`
(`event_core/live/runtime.py:286–331, 334–374`) and `_subject_history` (`:43–63`):

- **Subject history**: the compose context includes the subject's recent
  recognized moments (last ~12), **scope-gated to the target lane** — a public
  post never sees leadership-only context. In v5.1 this reads the streams +
  ledger instead of `detections`.
- **Compose-then-send**: copy is composed and the Discord send confirmed
  **before** the intent is marked fulfilled (at-least-once; `runtime.md`).
- **Meta-marker guard**: if the LLM returns a diagnostic instead of copy, fall
  back to the **deterministic renderer** (`render_intent`) rather than posting
  meta-text. The full marker list (`_META_MARKERS`, `runtime.py:265–278`),
  reproduced verbatim since Phase 4 deletes the source — each came from a live
  incident: `"skipping post"`, `"skip this post"`, `"would be stale"`,
  `"is stale"`, `"signal is from"`, `"signal data"`, `"data inconsistent"`,
  `"inconsistent with"`, `"live race is now"`, `"as an ai"`,
  `"unable to compose"`, `"cannot compose"`.
- **Naming guard**: composition receives the subject's current name via the
  identity layer (tag → `players.current_name`), never from stale payload copy.
- Per-stream subagents (§10) are the composing voice: the battle subagent frames
  battle patterns, the clan subagent clan dynamics, the war subagent race
  context via the war clock. Same guard rails for all.

## 8. Deliberate non-carries

| Current behavior | Disposition |
|---|---|
| `new_champion_unlocked` double-post path | dropped (`events.md` §6); rarity payload + ledger replace it |
| `battle_hot_streak` public posting | stays retired (redundant with `trophy_push`, `communication.py:26–31`) |
| Hard-coded channel ids in `runtime.py:17–21` | replaced by `DISCORD.md` lane lookup |
| Attendance nudges | **never built** (Q3: none, anywhere) |
| `leadership:` intents bypassing to the legacy scan (`runtime.py:364–371`) | replaced: leadership moments flow to the leader-action pipeline natively (`runtime.md`); the "leave it for the scan" workaround retires with Gen B |

## 9. Ratification record

One number in this doc is new rather than ported: **`arena_up` base score 85 with
bypass** (§4). Everything else is verbatim carry.
✅ **Ratified as drafted (Jamie, 2026-07-03).** No open items remain in this doc.
