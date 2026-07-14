"""Offline-only adapter for the retired deterministic proactive pipeline.

Production imports neither recognition orchestration nor intent delivery. This
adapter exists solely for migration rehearsals that explicitly compare the old
scorer/renderer behavior against archived data.
"""

from __future__ import annotations

from dataclasses import asdict


def run(conn, clock, now_iso: str, *, send_fn, compose_fn) -> tuple[dict, dict]:
    from engine import delivery, recognition

    clock_dict = asdict(clock) if clock is not None else None
    recognized = recognition.run_recognizers(conn, clock_dict, now_iso)
    delivered = delivery.consume(conn, send_fn, compose_fn, now_iso)
    return recognized, delivered


__all__ = ["run"]
