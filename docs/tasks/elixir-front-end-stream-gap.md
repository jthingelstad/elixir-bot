# Elixir Front-End Stream-Adoption Gap Assessment

Status: Analysis. Follows the data-flow gap assessment and the stream redesign
(Phases 1–5, 2b). The data layer now builds a rich, queryable event stream plus
derived state — per-mode battle activity (`summarize_battle_modes`), a fresh
war-season snapshot (`get_war_season_snapshot`), a season trajectory
(`get_season_window`), event windows/rollups, and durable `decision_cases`. This
assessment covers the LLM-facing **front-end** — interactive lanes, proactive
workflows, the read-tool surface, and the identity prompts — and where each still
operates signal-first instead of pulling from the stream.

Method: three independent read-only code traces. Evidence cited as `file:line`.

## Headline

The front-end is roughly half-migrated, and the gaps are overwhelmingly
**reachability, discoverability, and framing — not missing data**:

- The stream is barely *reachable* by tools (battle events can't be drilled into;
  the season trajectory is wired to no tool) and almost entirely *undiscoverable*
  (no prompt except `leader-lounge` even names `get_elixir_state`).
- Member-facing lanes (`ask-elixir`, `clan-chat`) were left signal/roster-first;
  only `leader-lounge` got the stream rewrite.
- The member-facing proactive post that should showcase non-war modes
  (`player-highlights`) narrates ranked/2v2 milestones using only overall trophy
  data.
- The identity (`SOUL`/`PURPOSE`/`GAME`/`CLAN`) still reads Trophy-Road + War;
  Ranked / Path of Legends, 2v2, and events are absent outside `awareness.md`.

## 1. Interactive lanes

| Lane | Class | Evidence |
|---|---|---|
| `leader-lounge` (clanops) | **STREAM-AWARE** | full `get_elixir_state` guidance (`prompt_builders.py:363-371`; `leader-lounge.md:13-18`) |
| `ask-elixir` | **PARTIAL → LEGACY** | never told the stream exists; `game_modes` reachable but untold; trajectory/war_season leadership-gated (`tool_exec.py:883-891`) |
| `clan-chat` / `general` | **LEGACY** | roster/donations voice (`general.md:17`); no stream pointer |
| `reception` | N/A | tool-free by design |

The gap is **guidance, not tools** — interactive has every read tool
(`workflow_registry.py:70,88`) but `_interactive_system` never names the stream
(`prompt_builders.py:292-320`). `ask-elixir.md:36-39` asks for
"trends/streaks/trajectory" with no pointer to source them. And season
trajectory / `war_season` is leadership-gated, so `ask-elixir` literally cannot
answer "what's our season trajectory."

## 2. Proactive workflows

| Workflow | Class | Note |
|---|---|---|
| war-awareness | STREAM-AWARE | via the awareness loop |
| weekly-recap | STREAM-AWARE (partial) | has war_season + stream + mode_pulse; missing `season_window` trajectory |
| memory-synthesis | STREAM-AWARE (partial) | has stream + war_season + cases; missing mode_pulse + season_window |
| **player-highlights** | **PARTIAL (top gap)** | ranked/2v2/event milestones use only overall trophy delta (`context.py:121-151`) |
| daily-insight | PARTIAL | stream used only as an anti-repeat guard (`_core.py:215-224`) |
| intel-report | LEGACY | our side framed with only a season label |
| war-recap | LEGACY (guardrail) | signal-only by design after the 04-19 misfire; season trajectory would help as *background-only* |
| season-awards / live-tournament / tournament-recap | LEGACY (correct) | closed-ledger / self-contained — leave as-is |
| promotion-content | LEGACY (low relevance) | roster snapshot |

Biggest live gap: **player-highlights** — a Path-of-Legends or 2v2 milestone is
narrated with overall trophy delta, not the player's mode trend, even though the
recap already proves per-mode context is safe and cheap (`_reports.py:688`).

## 3. Tool surface + identity framing

Tool reachability gaps:
- **Battle-grain events are undrillable.** `get_elixir_state` `recent_events`/
  `event_summary` hard-default to `event_class='signal'` with no override param
  (`event_stream.py:27`; `tool_exec.py:850,864`). Only the aggregated
  `game_modes` summary touches battle-class rows.
- **No per-member per-mode view from the stream.** `summarize_battle_modes` is
  clan-wide only (`event_stream.py:526`). Per-player ranked/2v2 trend is only
  available via the legacy facts-backed `get_member` `mode_activity` / `cr_api`.
- **Season trajectory (`get_season_window`) is wired to no tool**
  (`war_status.py:1150`); only the awareness loop sees it.

Discoverability: `get_elixir_state` is named in **zero** prompts except
`leader-lounge`. The whole stream-read surface is latent for the member lanes.

Identity framing (Trophy-Road + War):
- `GAME.md` has **no Game Modes section**; Ranked/PoL, 2v2, events absent.
- `PURPOSE.md` is war + arena-centric; "prefer signal over noise" (`:27`).
- `SOUL.md:43` "war-minded agent."
- Only `awareness.md` (and one `DISCORD.md` #player-highlights line) acknowledges
  Ranked.

Redundancy (consumption side): three per-mode reads now coexist —
`get_clan_game_modes` (facts-backed), `get_elixir_state game_modes`
(stream-backed), `get_member mode_activity` (facts-backed); and the `projects` /
`project_detail` aspects are identical (`tool_exec.py:888-891`).

## Sequenced plan

**Tier 1 — Reachability + discoverability** (highest value, low risk; mostly
prompt edits + small tool edits)
1. Tell the member lanes the stream exists: add a public stream-read paragraph to
   `_interactive_system` naming `get_clan_game_modes` + the public
   `get_elixir_state` aspects (`game_modes`, `recent_events`, `event_summary`,
   `event_rollups`), mirroring `leader-lounge`. *(Single highest-value fix.)*
2. Make battle events drillable: add an `event_class` parameter to
   `get_elixir_state` `recent_events`/`event_summary`.
3. Expose the season trajectory: add a public `season_window` aspect wrapping
   `get_season_window`, reachable by interactive, so `ask-elixir` can answer
   trajectory questions.

**Tier 2 — Feed per-mode into member-facing proactive posts** (high value)
4. `player-highlights`: add a per-member per-mode context (needs a `subject`
   filter on `summarize_battle_modes`) so ranked/2v2/event milestones cite the
   player's mode trend.
5. `memory-synthesis`: add mode_pulse + season_window so non-war arcs (a ranked
   push, a 2v2 duo) reach the long-term record.
6. `war-recap` + `intel`: add season trajectory / `war_season` as an
   explicitly-labeled *background-only* block (preserve payload-is-truth).

**Tier 3 — Reframe the identity** (prompt-only, medium value)
7. `GAME.md`: add a Game Modes section (Ranked / Path of Legends, 2v2, events,
   side modes / Merge Tactics) as first-class.
8. `PURPOSE`/`SOUL`/`CLAN`: broaden beyond war + Trophy-Road; soften "prefer
   signal over noise" → "prefer the stream's strongest story."

**Tier 4 — Consolidation** (hygiene)
9. Reconcile the three per-mode reads (pick one canonical path); collapse the
   duplicate `projects` / `project_detail` aspects.

## Guardrails

- Keep the deliberately-isolated signal-only workflows (season-awards,
  live-tournament, tournament-recap) untouched — their isolation is a correctness
  guardrail, and the war-recap/intel additions must stay *background-only* so the
  signal payload remains the source of truth.
- Tier 1 is almost entirely additive prompt/tool guidance — lowest risk, highest
  leverage. Start there.
