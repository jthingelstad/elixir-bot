"""Battle Intelligence Stage-A worker (docs/plans/battle-intelligence-1-data.md).

Computed enrichment only — no LLM. Extends ``battle_card_plays`` and
``battle_enrichment`` for un-enriched 1v1 battles each interval. Self-catching-up
and idempotent (the storage layer selects battles lacking an enrichment row and
uses ``INSERT OR IGNORE``), so this runs safely outside the engine tick and a
re-run or overlap is a no-op. The one-time historical backfill is
``storage.battle_intel.backfill`` (bounded chunks, run once at ship).
"""

from __future__ import annotations

import asyncio
import logging
import os

from runtime import status as runtime_status
from storage import battle_intel

__all__ = [
    "_battle_intel_stage_a",
    "_battle_intel_stage_b",
    "_battle_intel_prose",
    "BATTLE_INTEL_STAGE_A_MINUTES",
    "BATTLE_INTEL_STAGE_B_MINUTES",
    "BATTLE_INTEL_PROSE_MINUTES",
    "BATTLE_INTEL_BATCH",
]

log = logging.getLogger("elixir")

BATTLE_INTEL_STAGE_A_MINUTES = 15
BATTLE_INTEL_STAGE_B_MINUTES = 60
BATTLE_INTEL_PROSE_MINUTES = 30
BATTLE_INTEL_BATCH = 500
BATTLE_INTEL_PROSE_BATCH = 15


async def _battle_intel_stage_a() -> None:
    """Enrich a batch of un-enriched 1v1 battles (computed metrics + card plays).
    Telemetry carries real work-set counts so a caught-up run (``enrichment +0``)
    reads differently from a broken one."""
    runtime_status.mark_job_start("battle_intel_stage_a")
    try:
        result = await asyncio.to_thread(battle_intel.enrich_battles, BATTLE_INTEL_BATCH)
        runtime_status.mark_job_success(
            "battle_intel_stage_a",
            f"enrichment +{result['enriched']}, card_plays +{result['card_plays']}, "
            f"scanned {result['scanned']}",
        )
    except Exception as exc:
        log.error("Battle intel Stage A failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("battle_intel_stage_a", str(exc))


async def _battle_intel_stage_b() -> None:
    """Feature 2 (all $0, no model): profile new decks with the rules classifier,
    recompute the measured family matchup matrix, and fill expected_advantage /
    performance (the upset detector). Cheap enough to run hourly."""
    runtime_status.mark_job_start("battle_intel_stage_b")
    try:
        result = await asyncio.to_thread(battle_intel.rebuild_deck_intel)
        runtime_status.mark_job_success(
            "battle_intel_stage_b",
            f"profiled +{result['profiled']}, matchup_cells {result['matchup_cells']}, "
            f"expected_filled +{result['expected_filled']}",
        )
    except Exception as exc:
        log.error("Battle intel Stage B failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("battle_intel_stage_b", str(exc))


async def _battle_intel_prose() -> None:
    """Feature 3 (the only LLM spend): generate per-battle prose for GATED battles
    (allowlisted members ∩ battle_time >= min date). Off-switch: set
    ELIXIR_BATTLE_PROSE=0 to pause spend without touching the allowlist."""
    runtime_status.mark_job_start("battle_intel_prose")
    if os.getenv("ELIXIR_BATTLE_PROSE", "1") == "0":
        runtime_status.mark_job_success("battle_intel_prose", "disabled (ELIXIR_BATTLE_PROSE=0)")
        return
    try:
        result = await asyncio.to_thread(
            battle_intel.generate_prose_batch, BATTLE_INTEL_PROSE_BATCH
        )
        runtime_status.mark_job_success(
            "battle_intel_prose",
            f"prose +{result['prose_written']}, refreshed +{result['prose_refreshed']}, "
            f"scanned {result['scanned']}",
        )
    except Exception as exc:
        log.error("Battle intel prose failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("battle_intel_prose", str(exc))
