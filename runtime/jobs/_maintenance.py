"""DB maintenance, card catalog sync, and API sentinel jobs."""

__all__ = [
    "API_SENTINEL_POLL_MINUTES",
    "_format_size", "_build_maintenance_report",
    "_card_catalog_sync", "_api_sentinel_tick", "_db_maintenance_cycle",
]

import asyncio
import logging
import os

import cr_api
import db
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id
from runtime import elixir_log
from runtime import status as runtime_status
from runtime.helpers._common import _post_to_elixir
from runtime.signals.system import _post_system_signal_updates
from storage.api_sentinel import EVENT_SENTINEL_SIGNAL_TYPE, SCHEMA_SENTINEL_SIGNAL_TYPE


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


def _build_maintenance_report(size_before, size_after, purge_stats, backup_result=None, pruned_count=0):
    freed = size_before - size_after
    pct = (freed / size_before * 100) if size_before > 0 else 0
    rows_purged = sum(purge_stats.values())

    lines = [
        "**Weekly Database Maintenance**",
        "",
    ]

    # Backup section.
    if backup_result is not None:
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
    """Poll low-volume CR API discovery endpoints and publish first-seen drift."""
    runtime_status.mark_job_start("api_sentinel")
    try:
        baseline = await asyncio.to_thread(db.bootstrap_api_sentinel_baseline)
        events = await asyncio.to_thread(cr_api.get_events)
        if events is None:
            runtime_status.mark_job_failure("api_sentinel", "events API returned None")
            return

        pending = await asyncio.to_thread(db.list_pending_system_signals)
        sentinel_pending = [
            signal for signal in pending
            if signal.get("signal_type") in {EVENT_SENTINEL_SIGNAL_TYPE, SCHEMA_SENTINEL_SIGNAL_TYPE}
        ]
        if sentinel_pending:
            await _post_system_signal_updates(sentinel_pending, {}, {})

        event_count = len(events) if isinstance(events, list) else 0
        details = [f"events checked ({event_count} active)"]
        if baseline.get("bootstrapped"):
            details.append(
                f"baseline {baseline.get('observations', 0)} observation(s) "
                f"from {baseline.get('payloads', 0)} raw payload(s)"
            )
        if sentinel_pending:
            details.append(f"published {len(sentinel_pending)} sentinel signal(s)")
        runtime_status.mark_job_success("api_sentinel", "; ".join(details))
    except Exception as exc:
        log.error("API sentinel failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("api_sentinel", str(exc))


async def _db_maintenance_cycle():
    from scripts.backup_db import create_backup, prune_backups

    runtime_status.mark_job_start("db_maintenance")

    try:
        db_path = db.DB_PATH
        size_before = os.path.getsize(db_path)

        # 1. Backup before any destructive operations.
        backup_result = await asyncio.to_thread(create_backup)
        pruned = await asyncio.to_thread(prune_backups) if backup_result["ok"] else []
        if not backup_result["ok"]:
            log.error("DB backup failed: %s", backup_result["error"])

        # 2. Purge expired rows.
        purge_stats = await asyncio.to_thread(db.purge_old_data)

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
            size_before, size_after, purge_stats,
            backup_result=backup_result,
            pruned_count=len(pruned),
        )

        posted_to_log = await elixir_log.post_event_async(report)
        if not posted_to_log:
            try:
                channel_id = _get_singleton_channel_id("leader-lounge")
            except Exception as exc:
                runtime_status.mark_job_failure("db_maintenance", f"leaders channel config error: {exc}")
                return

            channel = _bot().get_channel(channel_id)
            if not channel:
                runtime_status.mark_job_failure("db_maintenance", "leaders channel not found")
                return

            await _post_to_elixir(channel, {"content": report})
            await asyncio.to_thread(
                db.save_message,
                _channel_scope(channel), "assistant", report,
                **_channel_msg_kwargs(channel), workflow="clanops",
                event_type="db_maintenance",
            )
        runtime_status.mark_job_success("db_maintenance", f"freed {_format_size(size_before - size_after)}")
    except Exception as exc:
        log.error("DB maintenance failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("db_maintenance", str(exc))
