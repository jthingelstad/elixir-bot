"""DB maintenance, card catalog sync, and API sentinel jobs."""

__all__ = [
    "API_SENTINEL_POLL_MINUTES",
    "_format_size",
    "_build_maintenance_report",
    "_card_catalog_sync",
    "_api_sentinel_tick",
    "_db_maintenance_cycle",
    "_scheduled_catch_up_cycle",
]

import asyncio
import logging
import os

import cr_api
import db
from runtime import elixir_log
from runtime import status as runtime_status
from runtime.helpers import (
    _channel_msg_kwargs,
    _channel_scope,
    _get_singleton_channel_id,
)
from runtime.helpers._common import _post_to_elixir

API_SENTINEL_POLL_MINUTES = int(os.getenv("API_SENTINEL_POLL_MINUTES", "240"))
log = logging.getLogger("elixir")


def _runtime_app():
    import runtime.app as app

    return app


def _bot():
    return _runtime_app().bot


def _format_size(size_bytes):
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.2f} GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1_024:
        return f"{size_bytes / 1_024:.1f} KB"
    return f"{size_bytes} B"


def _build_maintenance_report(
    size_before, size_after, purge_stats, backup_result=None, pruned_count=0, backups=None
):
    freed = size_before - size_after
    pct = (freed / size_before * 100) if size_before > 0 else 0
    rows_purged = sum(purge_stats.values())

    lines = [
        "**Weekly Database Maintenance**",
        "",
    ]

    # Backup section. `backups` is the per-database result list; name each one,
    # because "Backup: 1.2 GB -> 133 MB" gave no way to tell whether the
    # telemetry database was covered at all.
    if backups:
        ok = [b for b in backups if b["ok"]]
        failed = [b for b in backups if not b["ok"]]
        lines.append(f"**Backup:** {len(ok)}/{len(backups)} database(s)")
        for b in ok:
            lines.append(f"  {b['prefix']}")
        for b in failed:
            lines.append(f"  **{b['prefix']}: FAILED** — {b.get('error', 'unknown error')}")
        if pruned_count > 0:
            lines.append(f"  Pruned {pruned_count} old backup(s)")
        lines.append("")
    elif backup_result is not None:
        if backup_result["ok"]:
            compressed_mb = backup_result["size_compressed"] / 1_048_576
            original_mb = backup_result["size_original"] / 1_048_576
            lines.append(f"**Backup:** {original_mb:.1f} MB -> {compressed_mb:.1f} MB compressed")
            if pruned_count > 0:
                lines.append(f"  Pruned {pruned_count} old backup(s)")
        else:
            lines.append(f"**Backup: FAILED** — {backup_result.get('error', 'unknown error')}")
        lines.append("")

    lines += [
        f"**Before:** {_format_size(size_before)}",
        f"**After:** {_format_size(size_after)}",
        f"**Freed:** {_format_size(freed)} ({pct:.0f}%)",
        "",
    ]

    if rows_purged > 0:
        lines.append(f"**{rows_purged:,} expired rows** removed:")
        for table, count in purge_stats.items():
            if count > 0:
                lines.append(f"  {table}: {count:,}")
    else:
        lines.append("No expired rows to remove this cycle.")

    return "\n".join(lines)


async def _card_catalog_sync():
    """Sync the Clash Royale card catalog from the API."""
    runtime_status.mark_job_start("card_catalog_sync")
    try:
        api_response = await asyncio.to_thread(cr_api.get_cards)
        if not api_response:
            runtime_status.mark_job_failure("card_catalog_sync", "API returned None")
            return
        count = await asyncio.to_thread(db.sync_card_catalog, api_response)
        runtime_status.mark_job_success("card_catalog_sync", f"synced {count} cards")
        log.info("Card catalog sync complete: %d cards", count)
    except Exception as exc:
        log.error("Card catalog sync failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("card_catalog_sync", str(exc))


async def _api_sentinel_tick():
    """Poll low-volume CR API discovery endpoints so first-seen drift is
    RECORDED into api_sentinel_observations. Record-only: the store is the
    product team's data source (they read it directly) and the feed for the
    clan-facing game-level stream; it no longer posts drift to #leader-lounge."""
    runtime_status.mark_job_start("api_sentinel")
    try:
        baseline = await asyncio.to_thread(db.bootstrap_api_sentinel_baseline)
        events = await asyncio.to_thread(cr_api.get_events)
        if events is None:
            runtime_status.mark_job_failure("api_sentinel", "events API returned None")
            return

        event_count = len(events) if isinstance(events, list) else 0
        details = [f"events checked ({event_count} active)"]
        if baseline.get("bootstrapped"):
            details.append(
                f"baseline {baseline.get('observations', 0)} observation(s) "
                f"from {baseline.get('payloads', 0)} raw payload(s)"
            )
        runtime_status.mark_job_success("api_sentinel", "; ".join(details))
    except Exception as exc:
        log.error("API sentinel failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("api_sentinel", str(exc))


async def _scheduled_catch_up_cycle():
    """Run the registry's owed-period sweep without hiding individual failures."""
    from runtime.scheduled_catchup import run_catch_up_sweep

    runtime_status.mark_job_start("scheduled_catch_up")
    try:
        results = await run_catch_up_sweep(_runtime_app())
    except Exception as exc:  # noqa: BLE001 - the sweep itself must be visible
        log.error("Scheduled catch-up sweep failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("scheduled_catch_up", str(exc))
        return []
    counts: dict[str, int] = {}
    for result in results:
        outcome = result["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    runtime_status.mark_job_success("scheduled_catch_up", summary or "no eligible periods")
    return results


async def _db_maintenance_cycle():
    from scripts.backup_db import backup_all

    runtime_status.mark_job_start("db_maintenance")

    try:
        db_path = db.DB_PATH
        size_before = os.path.getsize(db_path)

        # 1. Backup before any destructive operations. backup_all covers every
        # database in the backup set, which is the same set `admin.sh restart`
        # covers — this used to call create_backup() bare and so backed up only
        # the clan DB, leaving telemetry snapshotted on restarts and never on
        # the weekly schedule.
        backup = await asyncio.to_thread(backup_all, log_progress=False)
        pruned = [p for r in backup["results"] for p in (r.get("pruned") or [])]
        for entry in backup["results"]:
            if not entry["ok"]:
                log.error("DB backup failed for %s: %s", entry["prefix"], entry.get("error"))

        # 2. Purge expired rows.
        purge_stats = await asyncio.to_thread(db.purge_old_data)

        # 2b. Memory expiry. purge_expired_memories has documented itself as
        # "called from db-maintenance" since it was written, and never was --
        # only a test called it (#215). Soft expiry hid rows from recall; this
        # is what actually reclaims them, closing the expand/backfill/cutover/
        # contract cycle for the inference TTL.
        import memory_store

        purged_memories = await asyncio.to_thread(memory_store.purge_expired_memories)
        if purged_memories:
            log.info("purged %s expired/retired memories", purged_memories)
        purge_stats["memories"] = purged_memories

        # 3. VACUUM reclaims disk space; must run outside any transaction.
        def _vacuum():
            conn = db.get_connection()
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()

        await asyncio.to_thread(_vacuum)

        size_after = os.path.getsize(db_path)
        report = _build_maintenance_report(
            size_before,
            size_after,
            purge_stats,
            backups=backup["results"],
            pruned_count=len(pruned),
        )

        posted_to_log = await elixir_log.post_event_async(report)
        if not posted_to_log:
            try:
                channel_id = _get_singleton_channel_id("leader-lounge")
            except Exception as exc:
                runtime_status.mark_job_failure(
                    "db_maintenance", f"leaders channel config error: {exc}"
                )
                return

            channel = _bot().get_channel(channel_id)
            if not channel:
                runtime_status.mark_job_failure("db_maintenance", "leaders channel not found")
                return

            await _post_to_elixir(channel, {"content": report})
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel),
                "assistant",
                report,
                **_channel_msg_kwargs(channel),
                workflow="clanops",
                event_type="db_maintenance",
            )
        runtime_status.mark_job_success(
            "db_maintenance", f"freed {_format_size(size_before - size_after)}"
        )
    except Exception as exc:
        log.error("DB maintenance failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("db_maintenance", str(exc))
