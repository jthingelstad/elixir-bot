"""Canonical signal identity helpers.

Every layer that records, routes, or marks a game signal must agree on the
same source key. Keep this module dependency-light so storage and runtime code
can both import it without creating package cycles.
"""

from __future__ import annotations

import hashlib
import json


def _clean_text(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def signal_type(signal: dict | None) -> str:
    return _clean_text((signal or {}).get("type") or (signal or {}).get("signal_type")) or "signal"


def signal_source_key(signal: dict | None) -> str:
    """Return the canonical, stable source key for one signal dict."""
    signal = signal or {}
    for key in ("signal_key", "signal_log_type", "source_signal_key"):
        value = _clean_text(signal.get(key))
        if value:
            return value.strip("|") or value

    parts = [
        signal_type(signal),
        _clean_text(signal.get("signal_date")) or "",
        _clean_text(signal.get("tag") or signal.get("player_tag") or signal.get("member_tag") or signal.get("target_player_tag")) or "",
        _clean_text(signal.get("season_id")) or "",
        _clean_text(signal.get("week") or signal.get("section_index")) or "",
        _clean_text(signal.get("day_number") or signal.get("period_index")) or "",
        _clean_text(signal.get("milestone") or signal.get("card_name") or signal.get("award_type")) or "",
    ]
    basis = "|".join(parts).strip("|")
    if basis:
        return basis

    payload = json.dumps(signal, sort_keys=True, default=str)
    return f"signal:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def batch_source_key(signals: list[dict] | tuple[dict, ...] | None) -> str:
    keys = sorted(signal_source_key(signal) for signal in (signals or []))
    payload = "|".join(keys)
    return f"batch:{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]}"


__all__ = ["batch_source_key", "signal_source_key", "signal_type"]
