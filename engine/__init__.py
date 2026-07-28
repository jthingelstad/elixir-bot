"""Elixir v5.1 engine — the single-generation replacement for Gens A/B/C.

The spec of record is ``docs/reference/v5.1/``. The live data tick has five
steps and deliberately stops before communication:

    engine.polling      step 1: adaptive API polling and raw-payload capture
    engine.ingest       step 2: battle mirror → battle_events
    engine.emitters     step 3: player / clan / war / calendar event emission
    engine.projections  step 4: current state, rollups, and war projections
    engine.management   step 5: evaluators and candidacy state machines

``engine.tick`` orchestrates those steps. ``engine.baselines`` and
``engine.clock`` provide their shared diff and war-time substrates.

Proactive communication is owned by the awareness loop — solely, since the
deterministic recognition/delivery stack was retired (#207). ``engine.offline``
remains as the API-free, Discord-free replay harness for the live tick path.

Public surfaces remain ``elixir_agent.py`` and ``runtime.app``; activity
wiring lives in ``runtime/activities.py``.
"""
