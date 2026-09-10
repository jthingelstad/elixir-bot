"""Elixir MCP client — the sibling data service (AGENTS.md "Elixir MCP").

The long-term direction is that elixir-bot does less of its own data
work and consumes Elixir MCP; this client is that seam. Phase 1
(2026-09-04, Jamie): member-stats answers go DIRECTLY to this source —
no shadow mode — with local tables as the error fallback only.

Error contract mirrors cr_api.py: every public helper returns the parsed
tool body on success and None on ANY failure (network, HTTP, tool error,
malformed body). Callers branch on None and fall back to local data —
nothing here raises across the module boundary. Tool errors and contract
drift are logged loudly so failures are visible in #elixir-log triage.
"""

import json
import logging
import os
import threading

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("elixir.mcp")

MCP_URL = "https://elixir.poapkings.com/mcp"
# The contract MAJOR this integration was built against. The contract's own
# semver rule (elixir-mcp packages/contracts) is that a breaking change is a
# major bump and a minor is additive, so a different major is worth a loud
# log line and a minor is not: pinning MAJOR.MINOR ("0.43") warned on every
# release the contract itself said was safe, and a pin that is always wrong
# stops being a signal. Calls still proceed either way — the fallback path
# covers real breakage.
#
# Moved to "1" on 2026-09-10 with the 1.0.0 shapes (notes[], applied{}) in
# capabilities/mcp_stats.py. Bump it when the changelog's `breaking` entry
# names a field this client reads.
PINNED_CONTRACT = "1"
_TIMEOUT_S = 15

_id_lock = threading.Lock()
_next_id = 0
_contract_warned = False


def _token() -> str | None:
    return os.getenv("ELIXIR_MCP_TOKEN") or None


def _rpc_id() -> int:
    global _next_id
    with _id_lock:
        _next_id += 1
        return _next_id


def call_tool(name: str, arguments: dict | None = None) -> dict | None:
    """One MCP tools/call. Returns the parsed tool body dict, or None."""
    token = _token()
    if not token:
        log.warning("elixir-mcp: ELIXIR_MCP_TOKEN not configured; skipping %s", name)
        return None
    try:
        resp = requests.post(
            MCP_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": _rpc_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        log.warning("elixir-mcp: %s transport error: %s", name, exc)
        return None
    if resp.status_code != 200:
        log.warning("elixir-mcp: %s HTTP %s", name, resp.status_code)
        return None
    try:
        envelope = resp.json()
    except ValueError as exc:
        log.warning("elixir-mcp: %s malformed response: %s", name, exc)
        return None
    if isinstance(envelope, dict) and "error" in envelope and "result" not in envelope:
        # A JSON-RPC-level refusal (rate limit -32029, unknown tool, bad
        # params) has no tool body at all; say what it was rather than
        # "malformed".
        rpc_err = envelope.get("error") or {}
        log.warning(
            "elixir-mcp: %s rpc error %s: %s %s",
            name,
            rpc_err.get("code"),
            rpc_err.get("message"),
            _describe_args(arguments),
        )
        return None
    try:
        content = envelope["result"]["content"][0]["text"]
        body = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("elixir-mcp: %s malformed response: %s", name, exc)
        return None
    if envelope["result"].get("isError") or isinstance(body.get("error"), dict):
        # The refusal's code is from the contract's closed set (invalid_tag,
        # not_entitled, not_recorded, not_found, quota_exceeded, ...). Log it
        # with WHAT was sent — the argument keys and the tag, never other
        # values — so the next refusal is readable here and not only in the
        # server's audit log (review 2026-09-10 §4.4: 32 invalid_tag refusals
        # on this surface in a week and not one line client-side).
        err = body.get("error") or {}
        log.warning(
            "elixir-mcp: %s tool error %s: %s %s",
            name,
            err.get("code"),
            err.get("message"),
            _describe_args(arguments),
        )
        return None
    _check_contract(body)
    return body


def _describe_args(arguments: dict | None) -> str:
    """Argument KEYS plus the subject tag, for a log line. Nothing else —
    the other values (queries, on_behalf_of ids) do not belong in a log."""
    args = arguments or {}
    parts = [f"args={sorted(args)}"]
    for key in ("player_tag", "clan_tag"):
        if key in args:
            parts.append(f"{key}={args[key]!r}")
    return " ".join(parts)


def _check_contract(body: dict) -> None:
    global _contract_warned
    if _contract_warned:
        return
    version = (body.get("meta") or {}).get("contract_version") or ""
    if version and version.split(".")[0] != PINNED_CONTRACT:
        _contract_warned = True
        log.warning(
            "elixir-mcp: contract drift — server %s, integration built for major %s "
            "(breaking = major, per the contract's semver rule); review the tool surface",
            version,
            PINNED_CONTRACT,
        )
