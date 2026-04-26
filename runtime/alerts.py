"""Admin alert signatures and posting helpers."""

from __future__ import annotations

import asyncio
import logging

import db
import prompts
from runtime import status as runtime_status
from runtime.helpers import _channel_msg_kwargs, _channel_scope

log = logging.getLogger("elixir")

_ALERT_SIGNATURES: dict[str, str | None] = {}


def _admin_mention_ref() -> str:
    from runtime import app as runtime_app

    name = db.format_member_reference("#20JJJ2CCRU")
    if not name or name == "#20JJJ2CCRU":
        name = "King Thing"
    if runtime_app.ADMIN_DISCORD_ID:
        return f"{name} (<@{runtime_app.ADMIN_DISCORD_ID}>)"
    return name


async def _alert_admin(content: str, event_type: str, signature: str) -> bool:
    from runtime import app as runtime_app

    if _ALERT_SIGNATURES.get(event_type) == signature:
        return False
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


def _clear_cr_api_failure_alert_if_recovered() -> None:
    api = (runtime_status.snapshot().get("api") or {})
    if api.get("last_ok") is True:
        _clear_alert("cr_api_auth_failure", "cr_api_outage")


def _cr_api_failure_signature() -> str | None:
    api = (runtime_status.snapshot().get("api") or {})
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
    api = (runtime_status.snapshot().get("api") or {})
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
    llm = (runtime_status.snapshot().get("llm") or {})
    if llm.get("last_ok") is True:
        _clear_alert("llm_outage")


def _llm_outage_signature() -> str | None:
    llm = (runtime_status.snapshot().get("llm") or {})
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
    from runtime import app as runtime_app

    loop = getattr(runtime_app.bot, "loop", None)
    if loop is None or loop.is_closed() or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_maybe_alert_llm_failure(context), loop)
    except Exception:
        log.warning("schedule_llm_failure_alert failed", exc_info=True)
