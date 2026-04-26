"""Signal delivery state persistence helpers."""

from __future__ import annotations

import asyncio

import db


async def _mark_signal_group_completed(signals):
    await asyncio.to_thread(_mark_delivered_signals, signals)
    for signal in signals or []:
        if signal.get("signal_key"):
            await asyncio.to_thread(db.mark_system_signal_announced, signal["signal_key"])


def _mark_delivered_signals(signals, *, today: str | None = None):
    for signal in signals or []:
        if signal.get("signal_key"):
            continue
        signal_date = signal.get("signal_date") or today or db.chicago_today()
        signal_type = signal.get("signal_log_type") or signal.get("type")
        if signal_type:
            db.mark_signal_sent(signal_type, signal_date)
        for member in signal.get("members") or []:
            member_log_type = (member or {}).get("signal_log_type")
            if member_log_type:
                db.mark_signal_sent(member_log_type, signal_date)
        if signal.get("type") == "clan_birthday":
            db.mark_announcement_sent(signal_date, "clan_birthday", None)
        elif signal.get("type") == "join_anniversary":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "join_anniversary", tag)
        elif signal.get("type") == "member_birthday":
            for member in signal.get("members") or []:
                tag = member.get("tag")
                if tag:
                    db.mark_announcement_sent(signal_date, "birthday", tag)


def _persist_signal_detector_cursors(cursor_updates):
    for update in cursor_updates or []:
        db.upsert_signal_detector_cursor(
            update.get("detector_key") or "",
            update.get("scope_key") or "",
            cursor_text=update.get("cursor_text"),
            cursor_int=update.get("cursor_int"),
            metadata=update.get("metadata"),
        )
