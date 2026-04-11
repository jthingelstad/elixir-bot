"""DB maintenance, card catalog sync, and daily quiz."""

__all__ = [
    "_format_size", "_build_maintenance_report",
    "_daily_quiz_post", "_card_catalog_sync", "_db_maintenance_cycle",
]

import asyncio
import os

import cr_api
import db
from runtime.app import bot, log
from runtime.helpers import _channel_msg_kwargs, _channel_scope, _get_singleton_channel_id
from runtime import status as runtime_status
from runtime.jobs._signals import _post_to_elixir


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


async def _daily_quiz_post():
    """Post the daily Elixir University quiz question."""
    from modules.card_training.views import CARD_TRAINING_CHANNEL_ID, post_daily_question

    runtime_status.mark_job_start("daily_quiz")

    if not CARD_TRAINING_CHANNEL_ID:
        runtime_status.mark_job_failure("daily_quiz", "CARD_TRAINING_CHANNEL_ID not configured for #card-quiz")
        return

    channel = bot.get_channel(CARD_TRAINING_CHANNEL_ID)
    if not channel:
        runtime_status.mark_job_failure("daily_quiz", f"channel {CARD_TRAINING_CHANNEL_ID} not found")
        return

    try:
        message = await post_daily_question(channel)
        if message:
            runtime_status.mark_job_success("daily_quiz", f"posted daily question (msg {message.id})")
            log.info("Daily quiz posted to #card-quiz (msg %s)", message.id)
        else:
            runtime_status.mark_job_failure("daily_quiz", "no question generated (card catalog may be empty)")
    except Exception as exc:
        log.error("Daily quiz post failed: %s", exc, exc_info=True)
        runtime_status.mark_job_failure("daily_quiz", str(exc))


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


async def _db_maintenance_cycle():
    from scripts.backup_db import create_backup, prune_backups

    runtime_status.mark_job_start("db_maintenance")

    try:
        channel_id = _get_singleton_channel_id("leader-lounge")
    except Exception as exc:
        runtime_status.mark_job_failure("db_maintenance", f"leader-lounge channel config error: {exc}")
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        runtime_status.mark_job_failure("db_maintenance", "leader-lounge channel not found")
        return

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
