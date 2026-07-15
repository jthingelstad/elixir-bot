"""Recognition orchestration — tick step 6 (runtime.md §2; recognition.md §1).

Battle + player streams feed one shared celebrate pipeline so same-tick
coalescing works across both (a trophy push and a level-up in the same tick
are one post, not two); clan and war moments take the direct path. Cursors:
'recognize:battle' tracks battle_events.rowid; the others track event_id.
"""

from __future__ import annotations

import logging

from engine.db import cursor_clear_failure, cursor_get, cursor_note_failure, cursor_set

log = logging.getLogger("elixir.engine.recognition")

POISON_SKIP_AFTER = 3


def _next_position(conn, table: str, current_pos: int) -> int | None:
    col = "rowid" if table == "battle_events" else "event_id"
    row = conn.execute(
        f"SELECT {col} FROM {table} WHERE {col} > ? ORDER BY {col} LIMIT 1",
        (int(current_pos or 0),),
    ).fetchone()
    return int(row[0]) if row else None


def _record_poison_skip(
    conn,
    *,
    consumer_key: str,
    table: str,
    position: int,
    exc: BaseException,
) -> None:
    try:
        from storage.messages import record_prompt_failure

        record_prompt_failure(
            f"Poison event skipped for {consumer_key} at {table}:{position}",
            "poison_event",
            "recognition",
            workflow="engine_recognition",
            detail=repr(exc),
            raw_json={
                "consumer_key": consumer_key,
                "table": table,
                "position": position,
                "skip_after_failures": POISON_SKIP_AFTER,
            },
            conn=conn,
        )
    except Exception:
        log.debug("failed to record poison-event skip", exc_info=True)


def _guard_cursor_section(conn, counters: dict, consumer_key: str, table: str, fn):
    try:
        result = fn()
    except Exception as exc:
        current_pos = cursor_get(conn, consumer_key)
        failed_pos = _next_position(conn, table, current_pos)
        if failed_pos is None:
            log.exception("recognizer %s failed with no pending row", consumer_key)
            counters[f"{consumer_key.split(':')[-1]}_error"] = repr(exc)
            return None
        count = cursor_note_failure(conn, consumer_key, failed_pos, exc)
        short = consumer_key.split(":")[-1]
        counters[f"{short}_poison_failures"] = count
        log.exception(
            "recognizer %s failed on %s:%s (%s/%s)",
            consumer_key,
            table,
            failed_pos,
            count,
            POISON_SKIP_AFTER,
        )
        if count >= POISON_SKIP_AFTER:
            cursor_set(conn, consumer_key, failed_pos)
            cursor_clear_failure(conn, consumer_key)
            _record_poison_skip(
                conn,
                consumer_key=consumer_key,
                table=table,
                position=failed_pos,
                exc=exc,
            )
            counters[f"{short}_poison_skipped"] = 1
        conn.commit()
        return None
    cursor_clear_failure(conn, consumer_key)
    return result


def run_recognizers(conn, clock: dict | None, now: str) -> dict:
    """Run all four recognizers over events since their cursors. Returns
    counters. Cursor discipline (runtime.md §2): each section advances its own
    cursor only after its work lands; a throwing section leaves its cursor
    unmoved and the idempotent ledger/dedup keys make re-processing safe."""
    from engine.recognition import recognizers as R

    counters: dict[str, int] = {}

    battle_result = _guard_cursor_section(
        conn,
        counters,
        "recognize:battle",
        "battle_events",
        lambda: R.battle_candidates(conn, now),
    )
    player_result = _guard_cursor_section(
        conn,
        counters,
        "recognize:player",
        "player_events",
        lambda: R.player_candidates(conn),
    )
    battle_cands, battle_pos = (
        battle_result if battle_result is not None else ([], None)
    )
    player_cands, player_pos = (
        player_result if player_result is not None else ([], None)
    )
    counters.update(R.run_celebrate_pipeline(conn, battle_cands + player_cands, now))
    if battle_pos:
        cursor_set(conn, "recognize:battle", battle_pos)
    if player_pos:
        cursor_set(conn, "recognize:player", player_pos)

    clan_result = _guard_cursor_section(
        conn,
        counters,
        "recognize:clan",
        "clan_events",
        lambda: R.clan_recognizer(conn, now),
    )
    if clan_result:
        counters.update(clan_result)
    war_result = _guard_cursor_section(
        conn,
        counters,
        "recognize:war",
        "war_events",
        lambda: R.war_recognizer(conn, clock, now),
    )
    if war_result:
        counters.update(war_result)
    game_result = _guard_cursor_section(
        conn,
        counters,
        "recognize:game",
        "game_events",
        lambda: R.game_recognizer(conn, now),
    )
    if game_result:
        counters.update(game_result)
    return counters
