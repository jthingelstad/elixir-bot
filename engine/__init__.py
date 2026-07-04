"""Elixir v5.1 engine — the single-generation replacement for Gens A/B/C.

Spec: docs/v5.1/ (architecture.md for the why; schema.md / events.md /
recognition.md / runtime.md / management.md for the what). The README
convention holds here too: if code and spec disagree, the spec wins and the
discrepancy is a bug.

Layout (runtime.md §2's seven steps, one module per responsibility):

    engine.db           connection + shared helpers (tags, time, hashing)
    engine.clock        the war clock — pure function over the race baseline (§16.2)
    engine.ingest       step 2: battle mirror → battle_events
    engine.baselines    state_baselines read/write (§8 diff substrate)
    engine.emitters     step 3: player / clan / war / calendar emitters
    engine.projections  step 4: current-state, form, rollups, war tables
    engine.management   step 5: evaluators + candidacy machines (management.md)
    engine.recognition  step 6: scorer, ledger, per-stream recognizers
    engine.delivery     step 7: intent consumer (at-least-once)
    engine.polling      the §15 adaptive budget scheduler (poll_state)
    engine.tick         the orchestrator: tick(now) runs steps 1–7
    engine.offline      OfflineEngine for the rehearsal harness (no API, no Discord)

Public surfaces stay `elixir_agent.py` and `runtime.app` (AGENTS.md facade
discipline); the engine is wired in through runtime/activities.py.
"""
