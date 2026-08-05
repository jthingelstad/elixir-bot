"""Admin alert signatures and posting helpers."""

from __future__ import annotations

import asyncio
import logging
import re

import db
import prompts
from runtime import elixir_log
from runtime import status as runtime_status
from runtime.helpers import _channel_msg_kwargs, _channel_scope

log = logging.getLogger("elixir")

_ALERT_SIGNATURES: dict[str, str | None] = {}
_DISCORD_USER_MENTION_RE = re.compile(r"\s*\(<@!?\d+>\)|<@!?\d+>")


def _without_user_mentions(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        lines.append(" ".join(_DISCORD_USER_MENTION_RE.sub("", line).split()))
    return "\n".join(lines).strip()


def _admin_display_name() -> str:
    name = db.format_member_reference("#20JJJ2CCRU")
    name = _without_user_mentions(name)
    if not name or name == "#20JJJ2CCRU":
        name = "King Thing"
    return name


def _admin_mention_ref() -> str:
    from runtime import app as runtime_app

    name = _admin_display_name()
    if runtime_app.ADMIN_DISCORD_ID:
        return f"{name} (<@{runtime_app.ADMIN_DISCORD_ID}>)"
    return name


async def _alert_admin(content: str, event_type: str, signature: str) -> bool:
    from runtime import app as runtime_app

    if _ALERT_SIGNATURES.get(event_type) == signature:
        return False
    if await elixir_log.post_event_async(_without_user_mentions(content)):
        _ALERT_SIGNATURES[event_type] = signature
        return True
    channel_configs = prompts.discord_channels_by_workflow("clanops")
    if not channel_configs:
        log.warning("Admin alert skipped (%s): no clanops channel configured", event_type)
        return False
    channel = runtime_app.bot.get_channel(channel_configs[0]["id"])
    if not channel:
        log.warning("Admin alert skipped (%s): clanops channel not found", event_type)
        return False
    await runtime_app._post_to_elixir(channel, {"content": content})
    await asyncio.to_thread(
        db.save_message,
        _channel_scope(channel),
        "assistant",
        content,
        **_channel_msg_kwargs(channel),
        workflow="clanops",
        event_type=event_type,
    )
    _ALERT_SIGNATURES[event_type] = signature
    return True


def _clear_alert(*event_types: str) -> None:
    for et in event_types:
        _ALERT_SIGNATURES.pop(et, None)


async def alert_discord_post_failure(surface: str, detail: str) -> bool:
    """Surface a Discord POST failure (typically a 403 after a permission change)
    to #elixir-log. Deduped per surface+detail so a recurring hourly failure
    alerts ONCE, not every tick. Pair with clear_discord_post_failure_alert() on
    the next success so a later re-break alerts again. This is the signal that was
    missing during the 2026-07-18 outage — posting perms were revoked and nothing
    said so."""
    admin_ref = await asyncio.to_thread(_admin_mention_ref)
    content = (
        f"{admin_ref} ⚠️ Elixir can't post to {surface} — likely a missing Discord "
        f"permission.\n{detail}\nCheck Elixir's permissions on that channel."
    )
    return await _alert_admin(content, f"discord_post_failure:{surface}", detail[:160])


def clear_discord_post_failure_alert(surface: str) -> None:
    """Reset the dedup for a surface once it posts again, so a future break re-alerts."""
    _clear_alert(f"discord_post_failure:{surface}")


async def _maybe_alert_job_failure(name: str, error: str) -> bool:
    admin_ref = await asyncio.to_thread(_admin_mention_ref)
    detail = (str(error) or "unknown error").strip()
    content = f"{admin_ref} ⚠️ Scheduled job `{name}` failed.\nError: `{detail[:180]}`"
    return await _alert_admin(content, f"job_failure:{name}", detail[:160])


def clear_job_failure_alert(name: str) -> None:
    """Re-arm the job's failure alert once it succeeds again."""
    _clear_alert(f"job_failure:{name}")


def schedule_job_failure_alert(name: str, error: str) -> None:
    """Fire-and-forget a #elixir-log alert for a failed scheduled job from sync
    code (mark_job_failure runs on worker threads). Deduped per job+error so a
    daily job failing the same way alerts once, not every run. Mirrors
    schedule_llm_failure_alert; best-effort — never raises into status recording."""
    from runtime import app as runtime_app

    loop = getattr(runtime_app.bot, "loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_maybe_alert_job_failure(name, error), loop)
    except Exception:
        log.warning("schedule_job_failure_alert: scheduling failed for '%s'", name, exc_info=True)


def _clear_cr_api_failure_alert_if_recovered() -> None:
    api = runtime_status.snapshot().get("api") or {}
    if api.get("last_ok") is True:
        _clear_alert("cr_api_auth_failure", "cr_api_outage")


def _cr_api_failure_signature() -> str | None:
    api = runtime_status.snapshot().get("api") or {}
    if api.get("last_ok") is not False:
        return None
    status_code = api.get("last_status_code")
    if status_code not in {401, 403}:
        return None
    last_error = (api.get("last_error") or "").strip()
    endpoint = api.get("last_endpoint") or "unknown"
    entity_key = api.get("last_entity_key") or "-"
    return f"{status_code}|{endpoint}|{entity_key}|{last_error[:160]}"


def _cr_api_outage_signature() -> str | None:
    api = runtime_status.snapshot().get("api") or {}
    if api.get("last_ok") is not False:
        return None
    if int(api.get("consecutive_error_count") or 0) < 3:
        return None
    status_code = api.get("last_status_code")
    last_error = (api.get("last_error") or "").strip()
    endpoint = api.get("last_endpoint") or "unknown"
    entity_key = api.get("last_entity_key") or "-"
    return f"{status_code}|{endpoint}|{entity_key}|{last_error[:160]}|{api.get('consecutive_error_count')}"


async def _maybe_alert_cr_api_failure(context: str) -> bool:
    api = runtime_status.snapshot().get("api") or {}
    admin_ref = await asyncio.to_thread(_admin_mention_ref)
    sent = False
    auth_sig = _cr_api_failure_signature()
    if auth_sig:
        content = (
            f"{admin_ref} Clash Royale API access just failed during {context}.\n"
            f"Last status: {api.get('last_status_code') or 'n/a'} on `{api.get('last_endpoint') or 'unknown'}` "
            f"for `{api.get('last_entity_key') or '-'}`.\n"
            "This usually means the CR API key or its IP allowlist needs to be updated."
        )
        sent = await _alert_admin(content, "cr_api_auth_failure", auth_sig) or sent
    outage_sig = _cr_api_outage_signature()
    if outage_sig:
        consecutive_failures = int(api.get("consecutive_error_count") or 0)
        content = (
            f"{admin_ref} Clash Royale API has failed {consecutive_failures} times in a row during {context}.\n"
            f"Last status: {api.get('last_status_code') or 'n/a'} on `{api.get('last_endpoint') or 'unknown'}` "
            f"for `{api.get('last_entity_key') or '-'}`.\n"
            f"Last error: `{(api.get('last_error') or 'unknown error')[:180]}`"
        )
        sent = await _alert_admin(content, "cr_api_outage", outage_sig) or sent
    return sent


_HARD_FAIL_LLM_MARKERS = (
    "usage limits",
    "usage limit",
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "billing",
    "quota",
    "credit",
    " 401",
    " 403",
)


def _is_hard_fail_llm_error(error_text: str | None) -> bool:
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in _HARD_FAIL_LLM_MARKERS)


def _clear_llm_failure_alert_if_recovered() -> None:
    llm = runtime_status.snapshot().get("llm") or {}
    if llm.get("last_ok") is True:
        _clear_alert("llm_outage")


def _llm_outage_signature() -> str | None:
    llm = runtime_status.snapshot().get("llm") or {}
    if llm.get("last_ok") is not False:
        return None
    consecutive = int(llm.get("consecutive_error_count") or 0)
    last_error = (llm.get("last_error") or "").strip()
    threshold = 1 if _is_hard_fail_llm_error(last_error) else 3
    if consecutive < threshold:
        return None
    workflow = llm.get("last_workflow") or "unknown"
    model = llm.get("last_model") or "unknown"
    return f"{workflow}|{model}|{last_error[:160]}"


async def _maybe_alert_llm_failure(context: str) -> bool:
    sig = _llm_outage_signature()
    if not sig:
        return False
    llm = runtime_status.snapshot().get("llm") or {}
    admin_ref = await asyncio.to_thread(_admin_mention_ref)
    consecutive = int(llm.get("consecutive_error_count") or 0)
    content = (
        f"{admin_ref} LLM API has failed {consecutive} time(s) in a row during {context}.\n"
        f"Workflow: `{llm.get('last_workflow') or 'unknown'}`, model: `{llm.get('last_model') or 'unknown'}`.\n"
        f"Last error: `{(llm.get('last_error') or 'unknown error')[:180]}`"
    )
    return await _alert_admin(content, "llm_outage", sig)


def schedule_llm_failure_alert(context: str) -> None:
    # This is the alert path of last resort — if it fails, an LLM outage and
    # the failure to report it both go unseen. Every exit short of a
    # scheduled alert logs at critical so elixir-v5.log still tells the story.
    from runtime import app as runtime_app

    loop = getattr(runtime_app.bot, "loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        log.critical(
            "schedule_llm_failure_alert: event loop unavailable; LLM failure during %s will NOT be alerted to Discord",
            context,
        )
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_maybe_alert_llm_failure(context), loop)
    except Exception:
        log.critical(
            "schedule_llm_failure_alert: scheduling failed; LLM failure during %s will NOT be alerted",
            context,
            exc_info=True,
        )
        return

    def _report_alert_outcome(fut) -> None:
        exc = fut.exception()
        if exc is not None:
            log.critical(
                "schedule_llm_failure_alert: alert coroutine failed for %s: %s",
                context,
                exc,
                exc_info=exc,
            )

    future.add_done_callback(_report_alert_outcome)


async def _alert_spend_ceiling(detail: str) -> bool:
    """Tell leadership the daily model-spend ceiling has been reached.

    Not an error, so it is phrased as a status: Elixir is doing less on purpose,
    hard posts are unaffected, and it clears at midnight UTC. Worth saying out
    loud precisely because the alternative — deck reviews quietly refusing while
    nobody is watching — looks exactly like a fault.
    """
    return await _alert_admin(
        "\U0001f4b0 **Daily spend ceiling reached** — "
        f"{detail}. Non-essential work (deck reviews, the daily insight) is paused "
        "until midnight UTC. Joins, farewells, role changes and clan-chat siblings "
        "are unaffected — those are never budget-gated. "
        "Raise `ELIXIR_DAILY_SPEND_USD` if this is biting too often.",
        "spend_ceiling",
        detail,
    )


def schedule_spend_ceiling_notice(detail: str) -> None:
    """Fire-and-forget the ceiling notice from sync code (the budget check runs
    on worker threads). Best-effort — a cost control must never raise into the
    call it is declining."""
    from runtime import app as runtime_app

    loop = getattr(runtime_app.bot, "loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_alert_spend_ceiling(detail), loop)
    except Exception:
        log.debug("could not schedule the spend-ceiling notice", exc_info=True)
