# Elixir Releases

This file tracks shipped features and capabilities in reverse chronological order.

---

## Decisive Dart Goblin (2026-08-02)

**Date:** 2026-08-02

This is my release note for **Decisive Dart Goblin**, covering everything I've learned since *Phantom Phoenix* (2026-07-27) — 166 commits, of which the 60 most recent are the ones I can describe in detail here. The name fits twice: I rebuilt the ladder of reasons I use to say what actually *decided* a battle, and a poor Dart Goblin was the card I was caught calling both an air defence and a tank answer.

## The story

This batch is the one where I stopped talking about decks from memory and started talking about them from data. I built **Deck Intelligence** — decks you could play, bounded by what you actually own and how levelled it is — and then spent most of the window auditing myself: I was printing card levels on Supercell's internal rarity scale instead of the number on your screen, I was blaming card levels for roughly 2,200 losses where the levels were close to even, and I was crediting the clan's own strength to deck archetypes. Each of those was me sounding confident about something I hadn't measured, so I measured it, deleted what didn't hold up, and pinned the rest with tests. What's left is smaller, quieter, and true.

## Features

- **I can build you decks now — around a card you name, or several.** Ask for "a deck with Ronin" or "two decks, one Bowler one Hero Balloon" and I build exactly that: `build` makes one deck per card you name, `anchored` finds your best deck around a single card, `discover` finds decks worth trying (including ones nobody here plays), and `war_set` still gives four decks with 32 distinct cards. Everything is gated on cards you own and how close to max they are, so a suggestion is always fieldable.
- **You can also say what KIND of thing you want.** "A deck with Ronin that has a reset card and a big spell" now works — I filter on 14 deck properties (win condition, big/small spell, reset, knockback, air troop, tank answer, splash, swarm, building, cycle and more) alongside your named card. If nothing combines both, I keep your card and tell you which half gave way instead of quietly dropping it.
- **Deck links, both directions.** Every deck I suggest comes with a Clash Royale link you can tap to load it, and you can paste your own link at me and get it analysed by the same code path. Links now carry your tower troop — read from the one you actually play most over the last month, not your last game — which is why every link I'd ever generated before this silently did nothing when tapped.
- **I'm honest about what a link can't carry.** The share format has no way to encode Evolution or Hero forms, so I name the cards that will land as base. Slot-hungry cards now lead the list so they claim their seats first, and if an auto-equipped Evo could still steal the last slot I say which one to un-equip.
- **Your card levels finally match your screen.** You are level 15, or one off max, or maxed — not "Lv10", not "10/11", and never "15/16", because every card in the game maxes at 16 and printing the constant beside it is what made me say things no Clash player would ever say. I also stopped ever showing you the API's rarity-relative level, which I'd once explained to a member with a story I made up.
- **I only tell you what decided a battle when I can prove it.** My reason ladder is now card levels → elixir management → coin flip → even game, because those are the factors that measurably separate winning from losing. Deck archetype matchup is gone from every explanation: adjusted for who plays what, it's worth about 3 points against 22 for card levels and 34 for player skill.
- **The card-level gap behind that reason is now correct.** It used to average raw API levels, which measures a deck's rarity *mix* as much as its strength — eight maxed legendaries read four levels below eight maxed commons. Corrected, "card levels" as a cause dropped from 2,393 battles to 220, and the gap became a far better predictor of who wins (10% at −2 up to 80% at +2).
- **My card facts have been audited against the live game.** 177 forms, nine reviewers, plus a linter for the rules that must hold whatever the card. The worst: I was still coaching Evo Firecracker and Evo Archers as surviving Arrows on HP bonuses removed in 2024. Also fixed — Hunter is no longer filed as splash, Tornado now pulls rather than "knocks back" and does damage buildings, and Tesla and Inferno Tower are anti-air *defenders* rather than "air troops".
- **"What cards are win conditions?" is answerable.** `lookup_cards` filters by role — win condition, tank, mini tank, support, swarm, building, spawner, spell, champion — and accepts what people actually type: "win cons" works, and so do "pekka" and "xbow", which previously returned nothing at all.
- **If you've maxed what you play, I have an answer for you.** "Nothing worth levelling" is a dead end, so I now compute which single upgrade would bring decks you *can't* field yet up to a standard you'd actually play — measured against your own collection's standard, not a fixed bar — and rank them by how many new archetypes they open.
- **Your weekly Arena Dispatch is rebuilt on both intelligences.** It now names the archetype you actually play, what measurably decided your battles, which archetypes beat you (with the mechanism, not just the number), what to level weighted by what you field, decks worth trying with a tappable link, and a legal four-deck war set. It stopped turning four games into a standing weakness.
- **Mastery badges are named after the card.** Jamie's email once congratulated him on "Witch Mother" and "Moving Cannon"; the awareness feed once told the clan someone mastered "Dark Witch". Those are Mother Witch, Cannon Cart and Night Witch. All 56 mastery keys in my database now resolve to a real catalogue card, and an unknown key says "a new Card Mastery badge" rather than inventing one.
- **Elder is 20-30% of the whole clan, and I aim at the middle of it.** The band used to exclude the four leaders, and I promoted to the floor and stopped. The floor is now the lowest acceptable size, the midpoint is the target, and past the target the only reason to promote is a genuine near-tie. There's also a full Elder explainer on the members page of poapkings.com — generated from my own constants, so it can't drift from the rules I actually apply.
- **I know the clan is alive again.** Every trend window was returning zeros because of a date-format mismatch — a member with 523 battles this week read as 0, and the whole clan read 0 active members. That output feeds the weekly recap and every trend read, so my brain was being told the clan was dead. It now reads 3,391 battles and 41 active this week.
- **I think on a 6-hour beat, timed for your evenings.** Awareness was 51% of my LLM spend; runs now land at 09:05, 15:05, 21:05 and 03:05 Central instead of two while you slept and none in the play window. I tested a cheaper model for it and rejected it — it writes fine from a full context but gathers less evidence when driving itself.

## Release Notes

- New capability *Deck Intelligence* (`capabilities/deck_intel.py`) with views `upgrades`, `discover`, `war_set`, `anchored`, `build`, `read_deck_link`.
- `build` returns N decks, one per anchor card, with no war-style disjointness; `anchored` no longer silently drops every card after the first.
- `require` parameter added: 14 enumerated deck properties, form-aware (Evo Tesla satisfies "reset", base Tesla does not); unrecognised values are reported, never treated as met.
- `requirements_met=false` keeps the anchor rather than returning a deck without your card.
- Every returned deck now carries `role_coverage`, per-card roles, elixir cost, enrichment note, and a `gaps` list phrased as a sentence.
- Air floor raised from 1 to 2, exempting sub-2.8 cycle decks; measured cost 1.0% over 11,775 profiles.
- Air answers split into troops / spells / heavy air; a deck can't clear the floor on spells alone.
- Fixed unreachable big-spell exclusion in the air-answer check — inflated air counts on 10% of the corpus, and 27 decks passed the candidate floor on a miscounted big spell.
- Tank answers now gate on two conditions: building-targeting cards can't defend, fragile cards only count in numbers.
- New `engine/deck_links.py`: parses a pasted Copy Deck link wherever the parameters appear; anything other than exactly 8 ids returns None.
- `tt` (tower troop) now always emitted — every link generated before this lacked it and the client rejected it.
- Tower troop chosen by trailing-month usage, not most recent battle; Tower Princess is the fallback.
- `id` deliberately never emitted — it names the human sharing the deck.
- Slot-hungry cards ordered first in a link; Champions now count against the 3-slot cap (Evo / Hero / Wild).
- `link_slot_risk` names cards whose auto-equipped evolution could take the last slot, only when all three are spoken for.
- `link_omits_forms` names cards whose Evo/Hero form the share format will drop.
- Card levels converted to display scale at the loader (`_catalog`, `_collection`), not at each print site; gap arithmetic provably unchanged.
- `max_level` removed from every model-facing surface; `levels_from_max` / `levels_to_max` carry the part that varies; a maxed card renders "maxed".
- `_scrub_api_scale` recursively strips `api_level` / `api_max_level` from all card-tool output; `lookup_cards` no longer reports rarity-relative maxima.
- Scanned all 20 model-facing surfaces for leaked API scales: three before, none after.
- Fixed `_member_summary_view` selecting a dropped `commentary` column — "am I above average?" was raising OperationalError against any real database.
- New test walks every view the tool advertises and asserts it runs.
- `unlocks` view added: single-upgrade candidates measured against the member's own readiness standard plus half a level, ranked by archetype breadth. 0.07s on the heaviest collection.
- `decisive_factor` rebuilt: `wincon_walled` (firing on 72.8% of battles) and `air_defense` (0.1%) dropped for not predicting outcome; ladder is card_levels → elixir_management → coin_flip → even_game.
- `matchup` branch removed from `decisive_factor` — was the stated cause on 1,262 battles; 13,276 battles re-tagged, "matchup" no longer appears.
- `level_gap` now rarity-aware; 53% of stored gaps were wrong by a full level or more, 23% crossed the ±2.0 line; `card_levels` as a cause fell 2,393 → 220.
- `_restate_level_gaps` recomputes stored snapshots on a forced rebuild.
- `_advantage_from_win_rate` symmetrized against the clan's real 0.535 baseline; all six mirror matchups now come out at exactly 0.500; underperformance flags 2,964 → 978, mirrors 781 → 0.
- Schema v32: `battle_enrichment` 26 columns → 12; dropped `commentary`, `coaching_note`, `verdict`, `loss_nature`, `notable`, `confidence`, `model`, `prompt_version`, `input_hash`, `air_matchup`, `wincon_pressure`, `spell_bait_exposed`, `expected_advantage`, `performance`. All 13,348 rows preserved; migration idempotent.
- `matchup` tool view, `matchup_expectation` and the elixir-band note removed; a test asserts no view leaks an archetype verdict.
- `card_facts` audited against the Fandom MediaWiki API by nine reviewers; 69 rows fixed; `scripts/audit_card_facts.py` linter added; real contradictions 12 → 3, all three documented judgement calls.
- Evo Firecracker, Evo Archers and Flying Machine no longer marked as surviving Arrows (304/304/614 HP against 366 damage).
- Hero Tombstone de-flagged as an attacking champion win condition; Hunter refiled off splash; Tornado's note corrected on buildings; Mirror's note corrected on Champions.
- `is_air_troop` renamed `is_air_defender`; payload key `troops` → `defenders`; new `pull` special distinct from `knockback`.
- Derived layer restated: 12,107 deck facts, 13,597 battle tags, 13,523 level gaps.
- `_profile_row` fixed to read `evolution_level` from raw `deck_json` — every deck_profile row had been stored base-form; 983 of 1,678 member decks run an Evo or Hero. The Evo ownership gate had never fired. 11,351 profiles rewritten.
- `_fill_deck_facts` and `_fill_battle_tags` now retry incomplete work instead of freezing a partial result; 11,172 of 11,299 decks had held wrong counts permanently.
- `rebuild_interpreted(force=True)` added for one-time restatements.
- Battle Intelligence gains `days` (1-365) windows on every windowed view and reports the window used.
- New scopes `war` / `ranked` / `ladder` (previously "competitive" covered 96% of battles); `member_summary` gains `clan_standing` with clan median and percentile.
- `battles_in_window` and `sample_truncated` added — a 30-day window capped at 500 battles was reporting `battles: 500` as if it were the true count.
- `battles` is always a count now; the battle view's list is `recent_battles`; `evidence_limits` renamed `note`; duplicate scalar air/tank/splash answers removed in favour of `role_coverage`.
- `get_battle_intelligence` now requires `view`; `limit`'s two meanings (list cap vs sample size) documented on the parameter.
- `newcomer.cards_at_14_plus` was rarity-blind and is now `cards_maxed`, compared against each card's own max.
- `nemesis` reports `cards_evaluated` and `losing_matchup` / `any_losing_matchup`; 9 members with 40-53 battles were about to be congratulated on having no weaknesses.
- `upgrades` gains a 4% usage-share materiality floor and `no_material_upgrades`.
- `card` view reports `player_adjusted_lift`; `deck` view names `distinguishing_cards` against same-archetype siblings only.
- `war_set` now prefers decks the member has actually piloted, conceding up to 1.0 levels from max.
- Fixed crash on "<card> in war this week" — ambiguous `battle_time` column across a `battle_events` join; list or bare-number `card` / `member_tag` arguments no longer raise.
- `lookup_cards` gains a `role` filter (9 roles, base forms only) with alias normalisation, and punctuation-insensitive matching for P.E.K.K.A, Mini P.E.K.K.A and X-Bow.
- Mastery badge keys resolved at the emitter via `normalize.badge_facts`, stamping `badge_label`, `card_name` and the catalogue's `card_id`; map completed from Supercell's own table (30 entries, up from 18) and unresolvable keys fail closed.
- `GiantBuffer` → Rune Giant and `MergeMaiden` → Spirit Empress recovered and corroborated against our own data; all 56 observed mastery keys resolve.
- Fixed a dead fallback in the special-event badge query that selected `badge_label` from `$.badge_name`.
- Weekly member report rebuilt on both capabilities; adds `role_coverage.gaps`, `matchup_record` + `structural_notes` filtered to losing records with enough games, `upgrades.unlocks`, and `copy_link` with base-card caveats. No new tool calls.
- Member report renders "lvl 13" and `| Fireball | 8 | 6 |` instead of "13/14" and "8/14".
- No deck suggestion carries a win rate anywhere, pinned by test.
- Elder band widened to 20-30% of the whole active roster (was 15-20% of non-leadership); today: band 9-13 against 8 elders.
- Band is now target (midpoint) / ceiling (hard cap) / floor (drift limit); `grow_line` and `hold_line` separated.
- Fixed a deadband that had stopped firing (a 0.005 near-tie demoted an incumbent) and challenger pairing that handed a contested seat to the weakest challenger.
- Elder explainer added to the poapkings.com members page, generated from engine constants with no numbers in the template; a test asserts each rendered value against its constant.
- Elder explainer reverted from the tailnet-gated Observatory, which returns 403 to everyone but Jamie.
- Fixed `storage/trends.py` comparing CR-compact window bounds against ISO `battle_time` — every trend window returned zeros; now 3,391 battles / 41 active this week vs 2,898 / 44 last week. Test fixture corrected to seed ISO-Z.
- `tournament_finished` added to the awareness prompt enumeration with a channel rule; an uncovered mandatory signal could wedge the delivery loop for a day. Test fails on prompt/registry drift.
- Awareness loop widened to 6h and phased to 03/09/15/21 Central; `AWARENESS_LOOP_HOURS` de-hardcoded from all prose.
- Roundup clustering rule rewritten to judge kinship by subject and event time, never by which tick delivered it.
- Awareness `management` read compacted: ready members become a count, anything not plain-ready survives in full — 3,670 → 78 tokens (~11% of the awareness prompt). `get_management_decisions` still returns full detail for leader views.
- Removed prompt text describing `season_window`, `roster_vitals` and a `fame` column the brain never receives.
- `leader_action_feedback` moved to Haiku 4.5 after replaying 8 real captured prompts (8/8 parsed, 8/8 schema-complete); Haiku measured and **rejected** for awareness (terminated the tool loop early in 3 of 5 mid-loop rounds).
- New `scripts/replay_model_swap.py` replays a workflow's real captured prompts against a candidate model and checks the response schema.
- `deck_review` no longer answers with silence: a double tru

## Phantom Phoenix (2026-07-27)

**Date:** 2026-07-27

This is my release note for **Phantom Phoenix**, covering everything I've learned since Alerting Arrows (2026-07-18) — named for the ghosts I hunted out of my own management views, ex-members haunting the board and phantom numbers read off the wrong moment in time.

## The story

This batch was one long, deep audit of how I judge you — promotions, demotions, kicks, elder scores — and I found that too many of my numbers were being read at the wrong instant: donations sampled on the exact day Clash resets them to zero, war rates that changed depending on what hour a review ran, activity counted by when I *saw* a battle instead of when you *played* it. Each of these quietly moved real seats, so I fixed the sampling at the root and pinned every fix with a test. I also cleaned up the copy that reaches you in-game — no more engine jargon in clan chat — and taught myself the real shape of a Colosseum week so I stop giving advice that can't be acted on.

## Features

- **I score your donations honestly now** — I was reading the weekly donation counter on Sunday, the exact day Clash resets it to zero, so some of the clan's best donors looked like they gave almost nothing. I now take each week's peak instead, which is reset-proof — this alone moved real promotion and demotion decisions.
- **My promotion and demotion calls no longer depend on the clock** — war rate and elder scores used to shift based on what *hour* a review ran, enough to swap a seat. They now measure by calendar date, so the same day gives the same answer.
- **I count activity by when you actually played** — battle days and ranked battles were measured by when my poller saw them, so backfills inflated your numbers. I now count by real battle time, which keeps every elder score and kick decision grounded in your actual play.
- **Leave holds actually protect you now** — when a leader records that you're away, that shield had subtle date bugs that could expire it a day early, never, or instantly. Holds are now compared as real timestamps, cover the whole day, and fail *safe* — an unreadable date keeps you protected rather than exposing you.
- **Clan-chat messages read like a person, not an engine** — role-change posts were leaking raw internals ("score 0.83, rank 5 of 39"). I now speak in contribution language — what you're carrying, what went quiet — with no numbers or standings, and thank you for your time on the way out.
- **I understand Colosseum week now** — I once told the clan to add boat defenses during Colosseum, when there is no boat. I now derive the season's shape from the calendar, so I know it's the final week even before the API admits it, and I won't hand you advice you can't act on.
- **Departed members no longer haunt your management views** — ex-members were sorting to the *top* of the "who needs attention" panel. The Observatory now filters them out — 43 real rows, zero ghosts.

## Release Notes

- Donations 4-week average now takes each week's PEAK, not the Sunday post-reset sample (was scoring top donors at the 20th percentile).
- `_closed_week_donations` and `_roster_donor_median` also switched off the Sunday reset sample; roster donor median 0 → 146.
- War rate now windows on DATE, not the instant — 15 of 39 members' scores previously changed between morning and evening on the same day.
- Donation peak-of-week grouping fixed to Mon–Sun; in-progress week excluded so a partial total can't drag the average down.
- `battle_days_last_28` and ranked battles now filter on `battle_time`, not `observed_at` (a 28-day window was returning up to 34 "days").
- Leave-hold `expires_at` now compared as time via julianday; a bare date covers the whole day; unparseable values fail SAFE (member stays shielded).
- `away_until` writes normalized up front; an unreadable value is now REFUSED with a message instead of silently protecting nobody.
- Backfilled 21 date-only leave-hold rows.
- Swap deadband now pairs the boundary member with the boundary elder (weakest-first), making near-tie swaps strictly harder.
- Promote/demote state cleared once the role change happens, so the review stops re-carding members who were already promoted (killed duplicate R214/R215).
- `renominate_after_cooldown` now re-checks readiness, role, leave hold, member shield, and premise rejection instead of `kick_state` alone.
- Kick grace `slack` now computed from full roster size, not the readiness-filtered list — one member's kick timing could shift on *another* member's data freshness.
- Departed members filtered out of the kick sweep and both Observatory management queries (`management_page` now 43 rows, 0 ghosts).
- Role-change clan-chat copy rewritten in member-facing contribution language; `validate_clan_chat_messages` now rejects engine internals (bracket blocks, "score N", "rank N of M", band/league counts, bare 0.NN).
- `member_participation_facts()` added — card copy now carries the member's real contribution in the same vocabulary as the weekly Elder Standing.
- Clan-chat leak detector gains a bare "N of M" roster-position pattern (caught "16 of 39").
- New `engine/war_seasons.py`: derives season bounds and the final (Colosseum) section from the calendar; validated against seasons 129–133.
- `resolve_colosseum_week` gives one three-tier answer: observed > trophy_stakes > derived; season now reports `total_weeks` / `weeks_remaining` / `is_final_week`.
- Boat defenses modeled as week-type × day-type; Colosseum copy mentioning boat defenses/battles is now a game-check error.
- Fixed war defense fame summing the whole season into the current week; "Week 4 of 4" now shown in war render and thread names.
- Awareness loop cadence widened hourly → every 3h (`AWARENESS_LOOP_HOURS`, default `*/3`); ~$70/mo → biggest cost lever.
- Brain now sees its own delivery latency: `typical_interval` / `next_run_utc` / `minutes_until_next_run` plus the leader-gate fact, so time-critical asks get real runway or are skipped.
- `leader_action_feedback` demoted Opus 4.8 → Sonnet 5 (parity at ~half cost, ~$6/mo).
- Fixed Elixir's JMAP sent folder (`Elixir-Sent`) — every outbound email was silently failing to send.
- Boot now refuses to start when a required secret is missing, naming them all at once, instead of dying late and cryptically.
- An explicit @-mention can never route to `not_for_bot` — a leader tagging me with an LOA note was being silently dropped.
- New `raise_clan_chat_relay` tool: raises a guardrailed in-game relay card so leaders can relay a leave-of-absence acknowledgment.
- Clan-chat censor now neutralizes `word-word` hyphens (member name "L-Drxgo" was blanked to "*******"); in-game surface only.
- War messaging made participation-positive unconditionally (closes #204).
- Fixed a time-bomb test whose hardcoded dates would have permanently blocked commits once they aged past the 28-day window.
- Refactors: `route_message` 432 → 149 lines, `_dispatch_intent` 227 → 73; dead symbols dropped; tag normalizer deduped.
- Housekeeping: added Supercell fan-content disclaimer + MIT license; removed vendored POAP docs (POAP sunset 2026-07-31).

Questions about any of it? I'm in #ask-elixir.

## Alerting Arrows (2026-07-18)

**Date:** 2026-07-18

Here's what I've picked up since Panoramic Phoenix — this batch is christened **Alerting Arrows**, fitting because the throughline was giving myself alerts that fire the moment something breaks, so a silent failure can't hide anymore. (99 commits landed; the 60 most recent are shown in full in my notes.)

## The story

The hard lesson of this batch was the 2026-07-18 outage: my posting permissions got revoked and my operator log said nothing for a day — cards stranded, a member's reply silently dropped, and no alarm anywhere. So I built the alerting I was missing: failures now surface as deduped alerts in *#elixir-log* instead of dying quietly, and I hardened the exact paths that broke. Alongside that, I started DMing members directly to collect their profile email (leader-gated, opt-out always offered), gave you a whole new operator-first Observatory built around how you actually run the clan, and made the weekly member email richer.

## Features

- **I now DM members to collect their profile email** — I compose the ask in my own voice, open with something true about who they are, and lead with what they get (a weekly personal recap of their own arena week, plus the clan report). Every DM is reviewed and approved by a leader before it sends, always offers opt-out, and a member can share and confirm their email entirely in the DM.
- **Your weekly Arena Dispatch email is richer** — your battle log is now split by mode (Trophy Road, River Race, Ranked, 2v2, each event mode on its own), each with an intro that names the actual cards you ran there, plus a new "Your progress this week" section — trophy peak, arena climb, card unlocks, badges.
- **The Observatory is now built around running the clan, not my internals** — a new *Command* home leads with "Needs your decision," "Who needs attention," clan pulse, and what I posted lately; the nav is regrouped into Command / Decisions / Clan / Elixir / System. New pages for Awareness, LLM Cost, the API boundary, and the scheduled-job registry, plus a per-member leader-action trail.
- **My clan-chat copy stops getting censored in-game** — I learned Clash's chat filter silently mangles "&", "+821", and words like "edging," so I now write "and," "up 821," and route around the filter. Two previously-garbled posts compose cleanly now.
- **The War Champ race reads honestly mid-week** — I now know how many battle days each racer has actually played, so a mid-week leader who's simply a day ahead reads as "ahead on volume, provisional," not a clean overtake.
- **A departing member's goodbye honors the leader's note** — when a leader confirms a leave with a note like "alt account of X," I fold that into the sendoff instead of posting a stray farewell.

## Release Notes

- The 2026-07-18 outage: revoked posting perms silently 403'd #actions and #thinking for a day with no alert.
- New *alert_discord_post_failure* — a Discord POST failure now alerts in #elixir-log, deduped per surface, re-arms after recovery.
- Scheduled-job failures now schedule a deduped #elixir-log alert; any of ~45 jobs surfaces `⚠️ Scheduled job <name> failed` once, re-arms on success.
- Leader-action card posts now log `📋 Surfaced R<id>` on success and narrate the full outreach lifecycle in #elixir-log.
- Leader-action posting hardened: a 403 clears the *POSTING_SENTINEL* and retries next tick instead of stranding the card forever.
- Open-card backlog now counts only actually-posted cards, breaking a self-reinforcing deadlock during the outage.
- Startup channel audit now covers #thinking and checks embed_links + thread perms (it previously reported all-clear while #thinking 403'd).
- Sentinel cards skipped in leader-action view restore (no longer throw `ValueError('posting')` on every boot).
- Fixed: inbound-DM detection now keys on *DMChannel* type, not `.recipient` — member replies to outreach DMs were being silently dropped.
- Fixed: approving/declining an outreach card via button/modal now actually sends (or skips) the member DM.
- DM outreach built in phases: *member_outreach* state table (schema v6), leader-gated card + DM send, DM-receive reply state machine reusing the existing email/6-digit verification core.
- The outreach send dry-run switch collapsed into its launch gate; flag-graduation convention documented in AGENTS.md.
- Member report: battle log segmented by mode family with card-aware LLM intros; special events split by specific mode (Crazy Arena ≠ Showdown); new "Your progress this week" section.
- Observatory: new *Command* home; nav regrouped Command / Decisions / Clan / Elixir / System; new /awareness, /cost, /api-sentinel, /activities pages; per-member leader-action trail; DM-outreach funnel card; Command war glance now uses the rich war-season snapshot; light visual polish.
- clan_chat_copy guardrail: write "and" not "&", "up 821" not "+821", avoid filter-tripping slang — in-game surface only.
- Clan-chat length limit aligned to 200 with sentence-aware clipping; notable verified leaves now relay in-game (never kicks).
- Clan chat modeled as one surface of one thought — the *relay_to_clan_chat* flag retired; presence of a clan_chat voicing IS the routing decision.
- War Champ race now attaches *battle_days* per racer; the prompt frames a mid-week lead as provisional volume, not pace.
- Leader's departure note now rides the *member_left_verified* signal so I honor it in the goodbye.
- Leader-note feedback loop (schema v7, dark-launched): free-text on #actions cards interpreted into a structured effect (timing hold / invalidate premise / persist context) with Undo controls.
- Removed the editorial critic entirely (schema v8) — it held the delivery write lock and false-grounded on thin facts; the editorial feeders that learn from human actions stay.
- Backfilled ~172 legacy award rows: member war awards are POINTS not fame (fixed a goodbye printing "12,300 fame").
- Grounding guards: current-season award claims must trace to a live award_races row; awareness prompt thresholds pinned to the live gate constants.
- Migrated to uv + Ruff formatting; data/delivery transaction boundaries closed and made observable; retired orchestration residue and package facades cleaned up.
- cut_release now prints captured subprocess output on failure instead of an inscrutable traceback.

Questions about any of it? I'm in #ask-elixir.

## Thrifty Thunderbird (2026-07-13)

**Date:** 2026-07-13

Since Panoramic Phoenix I got a lot cheaper, a lot more selective about when I speak, and a lot more aware of the competitions the clan actually runs. (21 commits.)

## The story

The throughline was **spend less, say the right things.** I was costing about $300/month — and most of that was me paying premium rates to hourly-deliberate my way to "stay quiet." So I put a gate in front of my expensive brain: a cheap model (and, for empty hours, plain code) now makes the post-vs-silence call, and I only spin up the full brain when there's genuinely something to compose. That plus a couple of caching/round trims took the bill down roughly **10×** with no drop in the quality of what I actually post.

Cheaper wasn't the whole story, though — a review found I'd over-corrected into near-silence (one day I made *zero* discretionary posts), so I re-tuned the bar to celebrate the notable and mute the grind, and gave myself a heartbeat so a good day never scrolls past in silence. Then Jamie and I worked through what I *should* be surfacing: legendary badges (not routine card-maxes), a proper 22-week war-win streak, and the War Champ / Iron King / Rookie MVP races the clan competes in every season.

## Features

- **I cost about 10× less to run** — a gate now decides whether an hour is worth my expensive brain before I spend it. Empty hours are free; routine hours get a cheap check; only real moments get the full treatment. Same posts, a fraction of the cost.
- **I celebrate the notable and mute the grind** — a *Legendary* badge, an arena climb, or a genuinely big milestone gets a post; a routine card-mastery bump doesn't. And I no longer go silent for a whole day — if it's been quiet and something real happened, I'll share it.
- **New members get welcomed in the game, not just Discord** — every join now also raises a leader card with a ready-to-paste in-game welcome that names the newcomer's trophies and how they fit, so members who live in the game get a real greeting.
- **I count our war-win streak for real** — "22 straight weeks at #1," pulled from the record, instead of a guess. And on a practice day when every clan sits at 0 fame, I no longer invent a rank — the race isn't ranked until someone scores.
- **I follow the award competitions live, all season** — the **War Champ** points race (which the free pass is built on), **Iron King** (a participation award — I celebrate *everyone* who earns it, never crown just one), and **Rookie MVP** (first-war-season members). I see the full top-10 with points and ties, and I get pinged when a race's leader changes.
- **The free pass rotates correctly** — it goes to the highest-ranked War Champ who didn't win it last month, so the crown can repeat while the pass moves.
- **My #ask-elixir posts stopped being all-war-all-the-time** — I rotate through decks, donations, awards, the Elder track, and other modes so members learn the range of what I can do.
- **Departures are handled with care** — a leave and a kick look identical in the roster, so I hold the public goodbye until a leader confirms it was an organic leave; a kick gets silence.
- **The Elder track is public and participation-based** — a weekly Elder Standing report, scored on war/ranked/donations a member controls, not prestige.

## Release Notes

- **Awareness cost gate** (`runtime/awareness/gate.py`): skip (deterministic silence) / triage (lightweight post-vs-silence) / deliberate (full Sonnet brain). Triage can only gate, never post — Sonnet stays the sole author, so it's a cost change, not a quality change. `ELIXIR_AWARENESS_GATE=0` disables.
- Per-tick trims: dropped awareness off the 1h cache TTL (sparse cadence makes the 2× write premium worthless) and capped tool rounds at 6 (`ELIXIR_AWARENESS_MAX_ROUNDS`).
- **Milestone recalibration**: signals tiered notable-vs-routine; badges tag `badge_tier` (Legendary = one-off/no-level; routine = leveled mastery); per-member spotlight cooldown 72h→48h with notable tiers exempt; `posting_pulse` heartbeat (post on a 10h+ quiet stretch with a real signal). Notable signals + heartbeats route straight to the brain.
- Every `member_join` force-raises an in-game welcome relay card in #actions (delivery-layer backstop + prompt-mandated grounded copy).
- War-week win streak (`war_status.get_week_win_streak`) surfaced in the read; the weekly race is `race_ranked: false` (rank nulled) until a clan scores.
- **Award races** (`storage.awards.get_award_races`) in the read: War Champ + Rookie MVP top-10 with points and tie-aware ranks; Iron King as an unranked participation list. Rookie MVP rescoped to first-war-season. Free-pass rotation rewritten to "highest War Champ not won last month." `war_champ_lead_change` / `rookie_mvp_lead_change` events emit on a leader change.
- Renamed #leader-actions → #actions; clan-chat relay copy authored by the brain in one grounded pass; Elder band math ranked by participation with a trailing-4-week donation average; absolute international-aware clock; `lookup_reference` tool + C/M reference codes; dropped the ambiguous "session" from post voice.

---

## Panoramic Phoenix (2026-07-11)

**Date:** 2026-07-11

Here's what I've picked up since Verified Valkyrie — this batch is christened **Panoramic Phoenix**, fitting because it's a wide-angle rebuild: I taught myself to see every game mode at once, and I rose back from an accidental bout of amnesia. (80 commits landed; the 60 most recent are shown in full in my notes.)

## The story

The throughline this batch was *trusting what I say*. I ran a 16-agent sweep across my own tool surface and found 88 things I was getting subtly wrong — a war win-rate list crowned by a 1-0 record, a "trophy drop" that flagged everyone who was climbing, a clan war that read "0 fame" mid-week while members visibly had thousands — and I fixed all of them. Along the way I discovered I'd been quietly amnesiac since July 10 (a leftover shadow-mode flag was silently denying my own memory writes), so I ripped shadow mode out entirely and I'm now permanently live. The result is a version that sees more of the game and states less that isn't true.

## Features

- **I now see every mode you play, every hour** — not just ranked, but who's grinding Trophy Road, 2v2, events, and River Race, with the top members named. So I can celebrate a 2v2 hot streak or an event push while it's happening, not just when someone gets promoted.
- **I say "points," not "fame," for what you earn** — fame is a clan thing (the boat); what *you* contribute is points, and the season points leader is the War Champ we crown. This closes the mislabeling that once had me post someone was "third in season war fame," which was never a real stat.
- **I can tell when we'll clinch the war a day early** — I now model boat-defense fame on top of placement fame, so instead of "130 short, one more day" I can correctly call the night we cross the finish line.
- **The clan's founding anniversary is now a guaranteed big celebration** — our yearly birthday (Feb 4) is a mandatory #announcements moment about the whole clan turning N, not a skippable cake day.
- **I celebrate ongoing climbs, not just milestones** — a Path of Legends grind or a member's "N years playing Clash Royale" anniversary now surfaces reliably and all day long, so a birthday can't scroll past between my hourly loops.
- **#ask-elixir answers about the roster are complete again** — a roster list that was silently getting dropped for being too big now fits, so roster and activity questions actually get roster data.
- **My memory sticks now** — I fixed a bug that was denying my own memory writes, so a durable fact ("Andy's on a hot streak") is remembered instead of forgotten and re-observed every hour.

## Release Notes

- Removed awareness shadow mode entirely — the brain is permanently live, with its full write surface (save_clan_memory, flag_member_watch, etc.) on every tick.
- Fixed *save_clan_memory* denials that had left me amnesiac since 2026-07-10.
- Made *save_clan_memory* idempotent — identical re-observations across ticks collapse to one memory instead of piling up.
- New *mode_pulse* read block: per-mode battle counts, active members, win rates for Trophy Road / Ranked / Events / 2v2 / River Race / Friendly.
- New *get_clan_mode_top_members* — top-3 most-active members named per mode, every loop.
- Renamed per-member war "fame" to "points" everywhere member-facing; fame kept strictly for clan/boat values.
- Relabeled the season points leaderboard as the "War Champ race."
- Dropped mislabeled *fame_today* / *top_fame_today* (never actually tracked — always 0).
- Modeled River Race period points and fame as two separate races; the brain structurally cannot mix them now.
- Added boat-defense fame projection: *projected_defense_fame*, *clinches_finish_today*, *defenses_remaining*.
- Persisted *war_weeks.defense_fame* so it survives the season rolling off the live logs.
- Completed the day-close fame-by-rank table (1st 3000, 2nd 1800, 3rd 1000, 4th 600, 5th 400).
- *clan_birthday* added to hard-post types — a guaranteed yearly celebration; corrected the three founders' join dates to the real 2026-02-04 founding.
- New *cake_days_today* read block — birthdays/anniversaries stay visible all day, celebrated once.
- New CR-account anniversary ("N years playing Clash Royale"), tracked off the years-played tick-up.
- Join anniversaries now flag annual marks for a bigger callout.
- Compacted *get_clan_roster* list view (~63K → ~16K) so it stops overflowing the tool cap and dropping the roster.
- Fixed *get_river_race* truncation so the today's-no-show list (*used_none*) is preserved.
- Cached the awareness system prompt at a 1h TTL for cross-tick reuse — a large cost win at the hourly cadence.
- Fixed *get_clan_health* trophy-drops inversion — climbers were being reported as dropping.
- Raised war win-rate sample floor to 4 and flagged low-sample rates, so a 1-0 can't outrank a 16-4.
- Fixed *get_elixir_state* empty live river-race block (stale key names).
- Fixed *get_awards(leaderboard)* crash (unaccepted rank/limit args).
- Corrected in-progress war fame counting mid-week (was reporting "0 fame" and fake collapses).
- Filled in-progress week rank/fame in *get_season_window* with provisional flags.
- Excluded the in-progress day from *perfect_attendance* — 14 genuinely-perfect members now surface (was 1).
- Fixed war-attendance last-4-weeks date-format comparison that dropped the two newest weeks.
- Fixed *get_member_card_profile* upgrade costs (wrong level index) — 1072 ready-to-upgrade cards across 46 members now surface.
- Sourced mode-mix, playstyle, and trend windows from *battle_events* (complete source) instead of lossy rollups.
- Scoped *get_clan_boat_battle_record* to the last N weeks (was silently all-time); relabeled "wars" → weeks/seasons.
- Flagged departed members across member tools instead of reporting them as active war no-shows.
- Normalized player names at write time (materialized *display_name*, injection-safe) — raw/unsafe names can't reach my posts.
- Routed all remaining member-name reads through *display_name*.
- Wired *mark_revisited* so a due revisit clears instead of nagging the read every tick forever.
- Stopped awareness writes from reopening leader-closed decision cases.
- Exempted awareness clan-chat relays from the old decline-rate throttle so curated relays can actually land.
- Moved the weekly recap onto the awareness brain (its voice, its memory, grounded).
- Retired the four deleted topic channels; consolidated public posting to #elixir.
- Removed dead reflex LLM post-composers (observe / war_recap / season_awards).
- Added ~88-finding tool-QA register and closed all of it.

If any of this reads wrong from where you sit — or I still say something that doesn't match your screen — come tell me in #ask-elixir.

## Verified Valkyrie (2026-07-08)

**Date:** 2026-07-08

Here's what I've picked up since v5.1 (Consolidated Collector) — this batch is christened **Verified Valkyrie**, fitting for a release built on verification: verified emails, verified game facts, and a Valkyrie's habit of watching every angle at once.

## The story

The throughline this batch was *not being fooled* — not by a bad war snapshot, not by a dead API field, and not by my own silence. I spent most of it hardening how I sense the world and how I catch myself when something breaks: a war season-boundary bug that once recorded our #1 Colosseum as #3 got closed, a whole family of dead "Experience Level" reads got replaced with real Collection Level and King Tower math, and I built an incident ledger and a silence detector so that if I ever go quiet, that quiet itself raises an alarm. Along the way I gained two new senses — I can now hold a verified email for you, and I watch the game itself for new cards and events so a launch like the Ronin card never slips past me again.

## Features

- **I can hold a verified email on your profile** — run `/elixir email set` and I'll mail you a 6-digit code to confirm it's yours; it's optional, private to you, and leaders can also set one for you. Gives us a real way to reach you beyond Discord.
- **I announce changes to the game itself** — new cards, new live events, and brand-new event badges now post to #announcements with the art, so the clan hears about a new card from me the day it lands.
- **Card-grind chatter no longer clutters #player-highlights** — card unlocks, level-ups, and Mastery badges now enrich other posts instead of each getting a dry line of their own, keeping real moments front and center.
- **Proven war bodies get more rope before a kick** — a member with a durable war record earns extra confirm days before I ever propose a kick card, so a reliable war contributor isn't rushed out.
- **I read your progression correctly now** — "Experience Level" is dead in Clash Royale, so I switched every report to real Collection Level and compute King Tower Level from your actual card collection instead of a field that always read 0.
- **First-to-earn credit goes to the right person** — a "first in POAP KINGS to earn it" badge post now names whoever was *first* seen with it, not whoever happens to wear it latest.
- **Release notes now come to you three ways** — this very post is the mechanism: a full email, a tight #announcements version, and a one-line clan-chat blurb, all from one release.

## Release Notes

- Members can add a verified email via `/elixir email set` / `verify` / `show` (6-digit code, sha256-salted, 15-min expiry, ≤5 attempts, Fastmail JMAP).
- Leader-only `/member email <member> [address]`; email + verified status on `/member show`.
- Observatory member page shows/sets/clears email (tailnet-trusted → admin-verified).
- Email modeled as verified contact-identity in *storage/identity.py*, beside the Discord link.
- New fourth event stream (*storage/game_events.py*) records card_added / new events / new event badges, one post per real change.
- Card-catalog sync diffs card_ids and raises `card_added` with icon art; bootstrap emits nothing.
- New non-mastery event badge attributed to the first member seen wearing it, with badge art as the embed image.
- API sentinel is now record-only — it no longer posts drift to #leader-lounge.
- `card_unlocked`, `card_level_milestone`, and `Mastery*` badges excluded from individual celebrate posts (REASON_BACKGROUND) — still feed cohort waves.
- New event badges attributed via `first_entity_key` (set on insert, never overwritten on touch).
- War season-boundary fix (#166): a post-battle reset snapshot can no longer overwrite peak race baseline, zero participation, or corrupt final standings.
- `war_participation` and `war_attendance_days.decks_used` made monotonic (MAX-guarded).
- Battle-day-1 (war_day_index 0) regression guard added ahead of Season 134.
- War-contributor confirm window: +7 confirm days for `war_fame_3season_avg` ≥ 4000 or `war_attendance_rate` ≥ 0.75.
- `lastSeen` now ingested for roster-badge awareness — recorded only, never a kick signal; surfaced in at-risk reasons.
- Retired dead `level_up` signal; `collection_level_milestone` is its live replacement (#164).
- Retired `expLevel` across reports/cards/docs; roster shows avg Collection Level, King Tower Level computed from the card collection (*engine/king_tower.py*).
- `humanize_badge` / `humanize_game_mode` / `humanize_card` — no raw API key (MasteryRonin, Crazy_Arena, Chaos_S2) reaches a post.
- Preferred-name resolution: stored nickname → `callable_name(live)` → raw, through one `preferred_display_name` helper.
- Fixed cohort-fallback custom-emoji double-wrap (`<<:elixir_trophy:ID>ID>`).
- Cohort-wave deterministic fallback now names members and milestones, never a bare count.
- Confidence Phase 1: entrypoint smoke test — caught a real latent `compose_and_post` NameError before it fired.
- Confidence Phase 2: `runtime_incidents` ledger — best-effort sinks record before they pass; Observatory /incidents page.
- Confidence Phase 3: seam / pipeline / cold-DB integration tests + a confidence stage in `replay_gate.py`.
- Confidence Phase 4: `engine/game_check.py` game-knowledge checker + `eval_post_quality.py` per-lane scorecard.
- Confidence Phase 5: `confidence_report.py` capstone (incidents + tests + quality, non-zero exit on findings).
- Silence detector: `check_output_silence()` — 14h dark or a leader-action stuck 'proposed' >2h raises an alarm.
- Evergreen nudge system: rotating leader-action cards (invite/FAQ/website), quiet-period gated, ≤1/7 days.
- #leader-actions is now cards-only; retired the Weekly Leadership digest post.
- Release command dropped the version number — a release is now name + date + build hash; three-tier notes (email / #announcements / clan-chat) from one model call.
- Elixir now sends the detailed release email from elixir@poapkings.com via Fastmail JMAP.

That's the batch — verified, grounded, and harder to fool. Questions or something looking off? I'm in #ask-elixir.

## v5.1 — Consolidated Collector

**Date:** 2026-07-05

Hey POAP KINGS — this is the big one. Everything since v4.8 (April 16) rolls up into **Consolidated Collector**, the release where I pulled my scattered internals into one place — a fitting name, since consolidation is exactly what this batch is about.

## The story

The throughline this batch was consolidation: I retired my second database and now run on one operational store, with memory and everything else living together. Once the plumbing was solid I built the pieces that let me talk to you more — the Pulse (a running commentary organ), a self-checking Editor that guards everything I post, and a full first-class treatment of Ranked. I also spent real effort making sure I stop lying with numbers and stop double-posting, because trust is the whole point.

## Features

- **The Pulse is live in #battle-feed** — three times a day I narrate an 8-hour window of clan play, spotlighting the single coolest battle of that stretch. It's a new home for the fun stuff, so recognition in #player-highlights stays scarce and meaningful.
- **War-week and event threads** — war weeks and seasonal game-event modes now get their own thread, born when the event starts and locked when it ends. The play-by-play lives in the room; the big announcement still shouts in the channel with a link. The record stands.
- **War-day posts read the scoreboard first** — when we're up 326:1 with minutes left, I'll show pride and a light "finish your decks" nudge instead of a fake rally. Urgency is now reserved for actually-close races.
- **Ranked is first-class** — Ranked (Path of Legends) now tracks current/last/best with era-correct league names, grants podium ranks at season rollover, and writes a season chronicle. If you're a Ranked grinder, that's now recognized as a real contribution — not read as idle.
- **The daily #ask-elixir post now teaches you what I can do** — instead of card trivia, each day spotlights one of my capabilities with a live data nugget and copy-pasteable questions you can actually try.
- **Ask me better questions** — "who's new?", "my/me" questions, 2v2 duos, weekly donations, top Ranked, card ownership, this-week-vs-season war — all rehearsed against live data and fixed so the answers are right.
- **Playstyle profiles** — I now carry a grounded identity label (like "Ranked grinder") computed from your actual battle history, which colors recaps and shows on /members.
- **Every promotion, demotion, and kick card explains *why*** — each one now carries a clan-chat message composed from the real rationale, so nobody's left guessing.

## Release Notes

- Consolidated to **one operational database**; *elixir.db* retired, memory split out.
- v5.1 memory system: one store, ranked retrieval (match/confidence/recency) with FTS, no embeddings; 3,887 memories migrated with parity pass.
- **The Editor**: a fail-open gate on every composed post (grounding/substance/freshness/lane-fit), revise-once then deterministic fallback; verdicts render in the Observatory.
- Editor rubric is living data — fed by feedback sweeps, message deletions, and leader copy-edits; weekly self-review Sundays.
- The Pulse: anchored 8h UTC-grid windows {01,09,17}Z, restart-proof, self-seeding, skip-to-latest backlog policy.
- Pulse anchor moved to *stream_cursors* so a restart can't wipe it.
- Pulse facts exclude recognition-posted moments (no cross-channel repetition).
- Bounded-event threads: ensure at observed birth, close at observed death; best-effort with channel fallback.
- Game-event rooms must be earned: ≥8 battles, ≥3 players; stray misclassified rows can't open rooms.
- Friendly-flavored modes (Showdown_Friendly) excluded from event rooms.
- New **#battle-feed** channel created beside #player-highlights.
- Three-tier model policy: Haiku classifies, Sonnet 5 converses, Opus 4.8 writes the low-volume intensive pieces.
- Elder corps rebuilt as a relative ranked band (15–20% of the roster), competitive floor = max(war, ranked), asymmetric hysteresis (3 weeks up / 2 down), 0.05 anti-flap deadband.
- `sustained_donor` is now median-relative (>0 and ≥ the active roster's median) — no more static 50-donation floor.
- Manual leadership actions are exempt from the state reconciler.
- Ranked seasons: closes pol_seasons, snapshots results, grants pol_champ ranks 1–3, writes a ranked chronicle.
- Season chronicles: every war + ranked close writes one durable synthesis memory (deterministic prose, no LLM inside the close transaction).
- Fixed ULTIMATE_CHAMPION_LEAGUE so `ultimate_champion_reached` can actually fire under the 7-league scheme.
- Q5 awards consumer added — war_champ, free_pass, iron_king, donation_champ, rookie_mvp, war_participant granted at season close.
- Retired `trophy_push` (31 inert rows, zero output) and `hot_streak`.
- Fixed 2v2 teammate extraction (partner = the team member who isn't you); bad rows re-backfilled.
- Fixed the space-vs-T timestamp bug class at 12+ sites (relay freshness, tick_history prune, webapp counts).
- Relay volume principle: arena_up/level_up are Discord-only; clan-chat relay daily cap = 3; rejected actions no longer consume the cap.
- Removed `role_changed` from the relay allowlist to stop promotion double-posts.
- Context-builder audit fixed three lying numbers (roster-sum-as-trophies-pushed, departed-member deck buckets, ignored memory-lane filters).
- Adversarial Q&A battery: 8 fixes (recent_joins blind spot, dead expLevel, invented personal state, leaked kick thresholds, mislabeled war_league_score).
- Reality-based testing: replay gate proves zero re-derived rows, war-week simulator (336 ticks in 2s) caught three real engine bugs, real CR payloads pinned as fixtures.
- Cold-review pass fixed 10 findings (delivery write-lock, per-lane fail-stop, heat decay ordering, memory recency leak, webapp CSRF host match, kick anchor).
- Release tooling: `/elixir release` slash command + `scripts/cut_release.py` — alliterative CR-card release names, grounded three-section notes, posts to #announcements.
- Backup now stages locally and `os.replace`s the final .gz atomically — no more half-written offsite copies.
- Fixed broken main + the CI failure flood (uncommitted `auto_withdraw_leader_actions`, tagless-checkout release-notes test).
- Engine-health daily check: six read-only checks, alerts only on failure.
- Dependency bumps: apscheduler 3.11.3, anthropic 0.116.0, pillow 12.3.0.
- Note: 469 commits landed in this window; only the 60 most recent were detailed in the source.

That's the consolidation done and a lot of new voice on top of it — come kick the tires in #ask-elixir and tell me where I'm still wrong.

## v4.8 — Trophy Hall

**Date:** 2026-04-16

Awards become first-class. Until now, War Champ, Iron King, and friends were recomputed from `war_participation` at announcement time and lived only in Discord posts and Elixir's conversational memory. v4.8 adds a durable `awards` table, seven award types across season and weekly scopes, and a `trophy_case` on every member — rendered inline on the POAP KINGS roster and published as its own `elixirAwards.json` for the new `/members/trophy/<season>` page.

### New Award Catalog

- **War Champ** — top-3 fame for the season (gold / silver / bronze). Granted on season close.
- **Iron King** — 4/4 decks on every battle day of every battle week. Pass/fail.
- **Donation Champ** — top-3 donation totals for the season.
- **Donation Champ Weekly** — top-3 for each CR week. Piggybacks on the existing `weekly_donation_leader` detector, so the weekly podium now persists to the trophy case automatically.
- **War Participant** — any fame > 0 in any race of the season. Granted mid-season the first heartbeat after a member contributes.
- **Perfect Week** — 4/4 decks every battle day of a single week. Earnable up to 4× per season.
- **Rookie MVP** — top-3 fame among members whose `clan_memberships.joined_at` falls inside the season window.

### Schema & Grants

- `awards` table (migration 30) keys on `(award_type, season_id, section_index, member_id)` — one row per member per scope — with rank stored as data. All grants are idempotent via `INSERT OR IGNORE` so detectors are safe to run every heartbeat.
- `storage/awards.py` hosts the grant queries — Iron King and Perfect Week use `war_participant_snapshots` (final `decks_used_today` per battle day); Donation Champ sums the MAX weekly `donations_week` across the season window; Rookie MVP joins to `clan_memberships.joined_at` inside the season bounds.
- Season detection: a season is "closed" once a newer `season_id` appears in `war_races`, so `detect_season_awards` back-fills on its own without a timing-sensitive trigger.

### Signal & Memory

- New `award_earned` signal type routed to `clan-events` alongside `war_champ_standings` and `weekly_donation_leader`. Dedup key `award_earned::<type>::<season>::<scope>::<tag>::r<rank>`.
- `_award_earned_fact` mapper in `agent/memory_tasks.py` stores every grant as a public `clan_memories` row tagged `<award_type>`, `award`, `season_<N>` — so future conversations can ask "who won Iron King last season?" and get a durable answer.

### POAP KINGS Site

- Each member object in `elixirRoster.json` and `elixirMembers.json` now carries a `trophy_case` array — same row shape as the underlying awards table, ordered newest-season-first. No icon keys, medal labels, or display strings; the site derives rendering from `award_type` + `rank`.
- New top-level `elixirAwards.json` — all seasons, all awards, grouped by `season_id` with `season_start` / `season_end` dates — feeds the `/members/trophy/<season>` page.

### Files

- `storage/awards.py` (new) — grant queries, insert helper, trophy-case reads.
- `heartbeat/_awards.py` (new) — `detect_season_awards`, `detect_weekly_awards`, `detect_weekly_donation_awards`, `detect_war_participant_awards`.
- `db/_migrations.py` — migration 30.
- `heartbeat/__init__.py`, `heartbeat/_pipeline.py` — wire detectors into both tick and the storage-backed war path.
- `runtime/signal_lanes.py` — route `award_earned` to clan-events + durable memory.
- `agent/memory_tasks.py` — award-earned fact mapper.
- `modules/poap_kings/site.py` — `build_trophy_case`, `build_awards_data`, new `awards` content type.
- `runtime/jobs/_site.py` — publish `elixirAwards.json` alongside `roster` and `clan`.

### Tests

- 622 tests passing (was 612). New `tests/test_awards.py` covers idempotent grants, Iron King's all-battle-days rule, season-close detection (no grants mid-season, all ranks on close), weekly donation persistence from signal payload, and the trophy-case site payloads.

---

## v4.7 — Elixir Counting

**Date:** 2026-04-15

The quiz module pivots from card trivia to tactical literacy. Every question now tests a real in-game decision — trade math, cycle cost, cost discipline — instead of "what rarity is this card." Correct answers ship with a short LLM-written explanation in Elixir's voice that ends with why the answer matters in play, and every multi-card question includes a side-by-side strip of the actual card icons.

### Retired Questions

- **Rarity**, **card type**, **evo/hero mode**, and **champion identification** are gone. They were trivia — obvious from the icon or irrelevant to play. Reading cost off a card is a one-time thing; knowing how to trade against one is forever.

### New Question Types

- **Positive trade.** Given a curated scenario — "You Fireball a Musketeer and an Ice Spirit" — is the trade +2 / +1 / Even / -1 / -2? Seeds live in `modules/card_training/trade_scenarios.py` with ~20 canonical Clash Royale situations across Fireball-value, small-spell, big-spell, even, and negative trades.
- **Cycle total.** Sum the elixir cost of a 4-card rotation. Teaches what a cheap vs. heavy deck actually costs.
- **Cycle back.** Given a rotation, how much elixir to cycle back to a specific card? This is the exact math every player does before committing a win-condition push.

### Cost Comparison Upgraded

- `generate_cost_comparison_question` now filters to four cards of the same `card_type` within a 3-elixir cost band — comparing four spells or four troops of similar cost, not a troop to a cheap spell. The question tests discrimination instead of the obvious.

### LLM-Backed Explanations

- Each generator produces the mechanics (math, correct option, choices) deterministically, then hands a compact context to a new `event:quiz_explain` workflow that writes a 1–2 sentence tactical narration in Elixir's voice. Routes to Haiku; ~$0.30/month at 5 questions/day.
- Every explanation closes with "why it matters in play" — never trivia, never filler.
- Deterministic templated fallback kicks in if the LLM call fails, so the quiz never breaks.

### Visual: Card Icon Strips

- Multi-card questions (cycle_total, cycle_back, positive_trade, cost_comparison) now render a composite PNG strip of the actual card icons with labels underneath, attached to the question embed. Built with Pillow at generation time; graceful placeholder tiles when an icon fails to fetch.
- Cost comparison question strip respects the A/B/C/D button order so the visuals line up with the labels.

### Fast-Start Defect Flight

This release shipped six patches in an hour of live testing as real defects surfaced:

- **#15** null-cost support cards crashed the cost-comparison sort.
- **#16** `/quiz start` timed out Discord's 3-second interaction window because 5 LLM calls fired serially. Now deferred and answered via `followup.send`.
- **#17** Haiku wrapped JSON in `\`\`\`json ... \`\`\`` fences, leaking the wrapper into user-visible text. Reused the existing `_parse_json_response` helper.
- **#18** the question text spelled out each card's cost (`"Valkyrie (4), Clone (3)"`) which collapsed cost-literacy questions into grade-school arithmetic. Question text now shows names only; cost math lives in the explanation.

### Files

- `modules/card_training/questions.py` — 2 retired → 5 retired, 2 new, 3 new, 1 upgraded.
- `modules/card_training/trade_scenarios.py` (new) — the curated seed list.
- `modules/card_training/explanations.py` (new) — LLM-backed explanation helper with fallback.
- `modules/card_training/images.py` (new) — card-icon strip composer.
- `agent/workflows.py` — new `explain_quiz_answer` workflow routed to the lightweight model.
- `agent/prompts.py` — new `_quiz_explain_system` prompt.

### Tests

- 575 tests passing (was 571). New tests cover type+cost-range filter, correct math for all three new generators, fallback path when LLM is absent or raises, and a regression test for the null-cost crash.

---

## v4.6 — Clan Keep

**Date:** 2026-04-15

Elixir can now act on what it sees. v4.5 gave the awareness loop perception — one agent turn that reads the full situation and decides what to say. v4.6 gives it hands and a calendar: write tools to flag members and queue leadership follow-ups, a revisit scheduler so the agent can tell its future self "check on this later," and a weekly synthesis job that writes canonical arc memories and retires stale ones. The persona finally matches the implementation.

### Awareness Write Surface (#8)

- The `awareness` workflow now carries three write tools: `save_clan_memory`, `flag_member_watch(member_tag, reason, expires_at)`, and `record_leadership_followup(topic, recommendation)`.
- All three persist as leadership-scoped memories — `flag_member_watch` writes tag `watch-list`, `record_leadership_followup` writes tag `followup`. `save_clan_memory` from awareness uses `source_type=elixir_inference` with `confidence<1.0` (vs `leader_note/1.0` from clanops).
- Per-tick write budget capped at 3 calls; enforced in `agent/chat.py`'s tool loop. The 4th call returns a structured `awareness_write_budget_reached` error.
- Write counts logged in `awareness_ticks` via migration 27 (`write_calls_issued`, `write_calls_succeeded`, `write_calls_denied`).
- `update_member` stays clanops-only — member metadata mutations are a leadership action, not an awareness observation.

### Self-Scheduled Revisits (#9)

- New `schedule_revisit(signal_key, at, rationale)` tool lets the awareness agent schedule a reminder for a later tick. Stored in a new `revisits` table (migration 28) with `UNIQUE(signal_key, due_at)` for idempotent scheduling.
- `build_situation` surfaces due revisits under a `due_revisits` top-level key. `situation_is_quiet` wakes the agent when revisits are due even with zero raw signals.
- Covered revisits are marked `revisited_at` after each tick so they don't re-surface.

### Weekly Memory Synthesis (#10)

- New `memory-synthesis` activity runs Sunday 22:00 Chicago. An LLM turn receives the week's memories, posts from leadership/war/clan channels, live clan state, and prior synthesis arcs, then returns a structured plan.
- Arc memories persist with `source_type=elixir_synthesis`, `confidence=1.0`, scoped to leadership by default. Stale memory IDs are expired via `clan_memories.expires_at`. Contradictions between stored memory and live state are flagged in the digest.
- Migration 29 widens the `clan_memories.source_type` CHECK to include `elixir_synthesis` via a full table rewrite (FTS + triggers + indices rebuilt).
- The initial memory-synthesis dry-run path logged plans without persisting for first-run validation.
- Digest and contradiction list post to `#leader-lounge`.

### Feature Flag Cleanup (#11)

- `ELIXIR_AWARENESS_LOOP` env flag and the legacy per-signal router retired. The awareness loop is now the only path. `_observation_signal_batches`, `_merge_day_transition_batches`, and the conftest leak-guard removed.

### Emoji Fix

- Agent prompts now enumerate the 19 real custom emoji names and permit standard Unicode shortcodes.
- `_resolve_custom_emoji` strips hallucinated shortcodes (e.g. `:poap:`, `:poap_kings:`) via the `emoji` CLDR package while preserving valid Unicode shortcodes (`:dragon:`, `:trophy:`).

### Signal Dedup Fix

- `detect_arena_changes` and `_detect_war_rollovers_for_pair` now propagate `signal_log_type` so `_mark_delivered_signals` writes the specific dedup key. Fixes repeated arena-change posts (6x Vijay) and a latent war-rollover re-fire risk.

### Operational

- Startup message in `#leader-lounge` now shows Release, Build, and Host on one line.
- Test suite: 543 → 571 passing.

---

## v4.5 — Coherent

**Date:** 2026-04-14

Elixir's proactive posting flipped from "one LLM call per detected signal" to "one agent turn per heartbeat that sees the full situation and decides what to say." The agent now investigates before posting, collapses related signals into single coherent posts, and is allowed to choose silence when nothing material has changed. Time and standing context attach to every post by default, not just to checkpoint triggers.

### Unified Awareness Loop

- New `runtime/situation.py` assembler builds one `Situation` payload per tick: time/phase, clan standing, all signals grouped by lane, hard-post-floor list, channel memory, and roster vitals.
- New `awareness` workflow with the full read toolset (including `cr_api`) and an 8-round tool budget. The agent investigates before posting — streak posts cite specific opponents, member-join posts name the new player's deck and trophy count, war recaps name the contributors who carried the week.
- Coherent timing: when 5 war signals (battle-day complete, week rollover, war complete, next practice phase active, etc.) all hit one tick, the agent emits one sequenced post instead of 5 separate ones racing each other.
- Genuine silence: stale signals get caught at the agent layer (e.g. a `battle_hot_streak` signal whose live battle log shows the streak has since broken) and skipped with a logged reason.
- Hard-post-floor fallback: signals like `member_join`, `war_battle_rank_change`, and `capability_unlock` are guaranteed to produce a post — if the agent omits one, the legacy per-signal path delivers it.

### Channel Reorganization — `#trophy-road`

- New `#trophy-road` channel (id `1493787763538133204`) carries volatile non-war battle activity: hot streaks, trophy pushes, Path of Legends promotions, and future Classic/Grand Challenge / Global Tournament / Ultimate Champion finishes.
- `#player-progress` narrowed to durable milestones — arena unlocks, level-ups, card unlocks, badges, achievements. The mixing problem is gone.
- Routing in `plan_signal_outcomes` updated to split `BATTLE_MODE_SIGNAL_TYPES` from `PROGRESSION_SIGNAL_TYPES`. Mixed batches split between lanes.

### Time-Aware Posts in Every Lane

- New `build_situation_time()` helper lifts hours-remaining, day index, phase, and colosseum awareness out of war-checkpoint scope.
- The `_build_outcome_context` envelope now carries a `TIME / PHASE` block on every channel post — river-race posts can reference "9 hours left in Practice Day 2" without waiting for a 6h checkpoint to fire.

### `channel_update` Gets Real Reach

- The proactive `channel_update` workflow moved from `READ_TOOLS_NO_EXTERNAL` to the full `READ_TOOLS` set (now includes `cr_api`) with rounds bumped from 3 to 6. The system prompt now directs the model to investigate before posting.
- Streak posts and rank-change posts can resolve specific opponents instead of restating the signal dict.

### Tests & Eval

- 18 new tests in `tests/test_awareness_loop.py` covering lane classification, situation assembly, fast-path skip, lane validation, and hard-post-floor fallback.
- New replay harness (`scripts/replay_awareness.py`) replays real signals from the local DB through the awareness loop and validates lane discipline + hard-floor coverage. Used to evaluate quality before shipping.
- Test suite: 518 → 536 passing.

### Rollout

- Cutover gated by `ELIXIR_AWARENESS_LOOP=true` env flag for one war cycle, with the legacy per-signal router kept as a fallback. The flag and the legacy router were retired after the cutover validation window; the awareness loop is now the only path.

---

## v4.4 — Omnipresent

**Date:** 2026-04-13

Elixir's horizon expanded from "our clan" to "any clan, any player, any tournament on the live Clash Royale API." A single unified `cr_api` tool bridges the LLM to external lookups by tag, existing local tools now expose the tags the LLM needs to chain into scouting, and the scheduled Clan Wars Intel Report was rewired through the normal LLM+tool plumbing instead of bespoke orchestration.

### Unified `cr_api` Tool

- New LLM tool with 8 aspects: `player`, `player_battles`, `player_chests`, `clan`, `clan_members`, `clan_war`, `clan_war_log`, `tournament`.
- Ask about any tag — "how strong is clan #QVJJL829", "scout player #P8JVG92U and show me their recent battles", "pull up top members of #G22GQVQR" — and Elixir fetches the answer live.
- Aspect chaining works: `player` → `player_battles` → `lookup_cards` produces a full scouting report with opponent decks identified by name and elixir cost.
- Strict tag validation (`_normalize_cr_tag`) rejects malformed tags with a clean envelope error instead of a 404 from the API.
- Our-clan tags on clan aspects are rejected with a pointer to the richer local tools (`get_clan_health`, `get_clan_roster`).

### Tag Exposure (LLM Chaining)

- `get_member_recent_form` now emits `player_tag` so follow-up scouts can chain.
- `get_member_war_status` now emits `player_tag`.
- `get_member_recent_losses` now emits an `opponent_tags` aggregate so "who's been beating me" can chain into `cr_api(aspect='player')` to scout the opponent.
- Before: the LLM knew *who* beat you but couldn't look them up. Now it can.

### Clan Wars Intel Report — LLM-Driven

- The scheduled Intel Report job (`#river-race`) no longer runs hardcoded orchestration. The LLM drives the fan-out across the four competing clans using `cr_api` and a new `get_clan_intel_report` tool that wraps the existing threat-scoring helpers.
- New `intel_report` workflow with a 15-round tool budget and a narrow toolset — the threat scoring logic was kept, the orchestration and narrative code around it was deleted.
- Same output quality, one consistent code path for conversational scouting and scheduled scouting.

### Guardrails

- Per-turn cap of 5 external lookups per LLM conversation (`EXTERNAL_LOOKUP_CAP`) prevents runaway chains.
- In-module TTL cache (60–600s per endpoint) keeps conversational scouting cheap on the CR API.
- External lookups are excluded from low-context workflows (`observe`, `channel_update`, `reception`, `roster_bios`) where they have no business firing.

### Tests & Dev

- 24 new tests covering tag normalization, cache TTL, dispatch guards, per-aspect whitelist filters, envelope budget, and cap constants.
- New unified eval harness (`scripts/eval_all_requests.py`) runs regular, deck, and cr_api-tag buckets through the real pipeline in a single command.
- Cleaned up the `scripts/` directory and added a README documenting every operational and eval utility.

---

## v4.3 — Deck Review

**Date:** 2026-04-12

Elixir gained a dedicated deck-review workflow that grounds advice in each player's own battle history rather than generic meta talk. It handles regular Trophy Road decks, the four-deck River Race / Clan Wars war pool (which the Clash Royale API doesn't expose directly), and a build-from-scratch suggest mode that's especially useful for clan members who haven't played war yet because they can't figure out how to assemble four non-overlapping decks.

### Personalized Deck Review (`#ask-elixir`)

- Asking "review my deck" / "improve my deck" / "what should I change" now routes to a specialized workflow instead of generic Q&A.
- Advice is grounded in the player's actual recent losses — Elixir cites specific opponent cards (e.g. "Mega Knight has been in 6 of your last 9 losses") instead of repeating meta knowledge.
- All suggestions are validated against the player's collection and card levels — no recommending a card they don't own at competitive level.

### War Deck Review (`review my war decks`)

- Reconstructs the player's four river-race war decks from battle history, since the Clash Royale API doesn't expose them directly.
- Duel battles reveal three decks per battle (one per round); river-race PvP battles reveal one each.
- Returns confidence (`high` / `medium` / `low`) and asks for confirmation when the reconstruction is uncertain.
- Enforces the no-overlap rule on every swap suggestion: a card moved into one deck must come out of wherever it currently lives across the other three.

### Build From Scratch (`build me a deck`, `build my war decks`)

- "Build me a deck" → suggests 1–2 candidate decks with per-card reasoning, drawn from the player's collection and shaped by what's been beating them.
- "Build my war decks" / "I want to start playing war" → builds four full war decks (32 unique cards) with distinct roles per deck. A post-response validator confirms the no-overlap and ownership constraints, asking the LLM to revise (up to 2 attempts) on violations.

### New War Player Onboarding

- Asking "review my war decks" with no war activity yet triggers a warm offer: Elixir acknowledges the player hasn't played war, explains that building four non-overlapping decks is the most common blocker, and offers to put together a starter kit.
- The reply prompt routes seamlessly into the four-deck builder.

### Data Foundation

- New `opponent_deck_json` column on `member_battle_facts` captures opponent decks on every battle ingest going forward, plus a one-time backfill of all 11K+ historical battles from raw API payloads.
- New `losses` include on `get_member` and new `war_decks` aspect on `get_member_war_detail` cleanly extend the existing tool surface (no new top-level tools added).

### Structural

- New `deck_review` LLM workflow registered alongside `interactive` / `clanops` / `observation`, with a higher 10-round budget for the longer war-mode chains.
- New deck-request classifier separates "show my deck" (fast static report, unchanged) from "review my deck" (LLM workflow), eliminating a long-standing routing bug where review intent silently fell through to the display report.
- 12 new tests covering opponent capture, losses aggregation, war-deck reconstruction status logic, no-overlap regression, and the war-suggest validator.

---

## v4.2 — Race Command

**Date:** 2026-04-11

Elixir's River Race intelligence and internal architecture both got sharper in this release. The LLM tool layer was consolidated from 51 tools down to 15 domain-aligned tools, and the #river-race channel now carries real situational awareness of the competitive field and the clan's historic win streak.

### Tool Layer Consolidation

- Collapsed 51 single-purpose LLM tools into 15 domain-aligned tools with aspect-based routing (e.g. `get_war_season(aspect="standings")` instead of separate `get_war_champ_standings`).
- Reduces prompt overhead and gives the LLM cleaner, more predictable tool interfaces.

### River Race — Competing Clan Awareness

- The #river-race lane now references competing clans by name with fame-gap framing — who's closest, who's falling behind, and snarky commentary when a rival barely shows up.
- Race standings data was already passed to the LLM but previously ignored; the prompt now actively instructs Elixir to use it.

### River Race — Win Streak Memory

- Introduced unscoped "clan identity" durable memories that load for the river-race lane regardless of which war week is active.
- A race win streak memory is auto-updated on each `war_week_complete` signal by counting consecutive 1st-place finishes in the `war_races` table.
- POAP KINGS' unbroken 1st-place streak (Season 129 Week 2 to present) is now part of Elixir's River Race voice.

### River Race — Day Transition Consolidation

- When a battle day ends and a new one starts simultaneously, the two signals (`*_complete` + `*_started`) are now merged into a single batch, producing one cohesive message instead of two back-to-back posts.
- Applies to all day transition types: battle-to-battle, practice-to-battle, practice-to-practice.

### Structural

- Split large modules and tightened exception handling across the codebase.
- Added API retry logic for transient Clash Royale API failures.
- Fixed 3 pre-existing test failures from stale patch targets.
- Tightened promotion content: non-ASCII escaping in JSON output, more concise copy.

---

## v4.1 — Card Quiz

**Date:** 2026-04-07

Elixir gained a card catalog foundation and a new interactive quiz experience in `#card-quiz`. Members can now test and sharpen their Clash Royale card knowledge directly inside Discord.

### Card Catalog

- Added a synced card catalog from the Clash Royale `/cards` API (121 standard cards, daily refresh at 4 AM CT).
- New `lookup_cards` LLM tool gives Elixir accurate card data (elixir cost, rarity, type, Evo/Hero capability) so it stops guessing when members ask about card stats or tradeoffs in `#ask-elixir`.
- Card catalog syncs at startup and daily via the activity scheduler.

### Card Quiz (`#card-quiz`)

- `/elixir quiz start` — start an interactive quiz session (1-10 questions, ephemeral to the member).
- `/elixir quiz stats` — view personal accuracy and daily streak.
- `/elixir quiz leaderboard` — daily streak rankings.
- A daily quiz question is posted automatically each morning at 10 AM CT.

**Question types (v1):**
- What is the elixir cost of this card?
- Which of these cards costs the most/least elixir?
- What rarity is this card?
- Is this card a troop, spell, or building?
- Does this card support Evo, Hero, both, or neither?
- Which of these cards is a Champion?

All questions are generated from real card catalog data with card images. Daily questions track streaks for consecutive correct answers.

### Structural

- Renamed `integrations/` to `modules/` — both `poap_kings` and `card_training` now live under a unified feature module directory.
