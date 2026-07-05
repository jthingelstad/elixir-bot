# Elixir v5.1 — Channel Architecture & Bounded-Event Threads

> **Status:** 🟡 Spec'd 2026-07-05 (Jamie: "Let's do the threads. Good idea.
> Very discord native."). Build after the season-close weekend; first thread
> at season 134's opening war week.
> **Owner:** Jamie · **Last worked:** 2026-07-05

## 1. The standing principle: one lane per stream

Ratified 2026-07-05 (the architecture had already converged here):

| Stream | Channel | Content |
|---|---|---|
| player | #player-highlights | earned recognition — the trophy case, scarce |
| clan | #clan-events | joins, milestones, calendar, season awards |
| war | #river-race | war ops: day opens, week/season ceremony |
| battle | #battle-feed | the Pulse + battle entertainment (pulse.md) |

A post's stream determines its home. New surfaces must name their stream
before they name their channel.

**Ops note (learned the hard way, 2026-07-05):** adding a channel touches
TWO registration points — the DISCORD.md section AND
`prompts.CHANNEL_LANE_CONFIG` — and the startup validator hard-fails on
unknown lanes (a miss = on_ready crash; cost a 4-minute partial outage).
Threads bypass all of this — one reason they won.

## 2. Bounded-event threads (Jamie's concept, Discord-native form)

Fixed-duration events get a **thread** as their room — born at the event's
observed birth, locked+archived at its observed death. The discovered-
lifecycle philosophy made spatial: the thread IS the bounded event's
chatroom, and its archive IS the browsable record.

**The reach/tidiness split (the middle path, ratified with the thread
decision):** *announcements shout in the permanent channel; play-by-play
lives in the thread.* Elixir's opening post in each thread names the
participants who matter (joining them into notifications); the closing
ceremony posts in the channel with a link into the thread.

### v1 thread kinds

| Event | Parent | Born when | Named | Dies when |
|---|---|---|---|---|
| War week | #river-race | section start observed (war emitter) | "War Week {n} — Season {id}" / "Colosseum — Season {id}" | week_finished → closing post in-thread, then lock + archive |
| Seasonal game event | #battle-feed | a new special_event mode first seen in battle_events (e.g. Event_RestlessDead) | game-mode display name | mode absent from the stream ≥3 days → quiet lock |

Not in v1: ranked-season threads (the podium post suffices), tournament
threads (dormant surface). Add by ratification, per the pattern rule.

### Routing rules

- IN THREAD: war_day_opened, day-level recaps/nudges, event-mode chatter
  hooks — the daily texture that would otherwise stack up in the channel.
- IN CHANNEL (+ thread link): week_finished, season ceremony, anything
  relay-eligible. Reach where it counts.
- The Pulse stays in #battle-feed proper (it is not a bounded event).

### Mechanics

- `war_weeks.thread_id` (additive column; likewise a small
  `event_threads` table for game-event threads: mode name, thread_id,
  born_at, locked_at).
- Delivery: `communication_intents` gains optional `thread_id`; when set,
  the send targets the thread instead of the lane channel. Lane stays the
  parent (permissions, voice, Editor context all unchanged) — the
  single-pipeline rule holds; a thread is a *delivery address*, not a lane.
- Auto-archive duration: 3 days (war-day cadence); Elixir locks explicitly
  at close ("the record stands") so revival is deliberate, not accidental.
- Failure posture: thread creation is best-effort — if it fails, posts
  fall back to the parent channel (never block the ceremony on a room).
- Chronicles record the thread id at close — the memory and the archive
  reference each other.

## 3. Build plan

1. `runtime/threads.py`: create/lock helpers (bot API), best-effort
   wrappers; `war_weeks.thread_id` + `event_threads` DDL (schema_v51 +
   live CREATE).
2. War emitter hook: section-start → create thread, store id; opening
   post names the week's likely fighters (last week's participants).
3. Delivery `thread_id` routing + intent plumbing (small).
4. week_finished path: closing post in-thread → lock; channel post links.
5. Game-event watcher in the pulse job's window pass (it already reads
   the mode mix): new special_event mode → thread; ≥3-day absence → lock.
6. Tests: lifecycle (born/locked once, idempotent across restarts),
   fallback-to-channel, routing split per §2.
