"""Elixir MCP client — the sibling data service (AGENTS.md "Elixir MCP").

The long-term direction is that elixir-bot does less of its own data
work and consumes Elixir MCP; this client is that seam. Phase 1
(2026-09-04, Jamie): member-stats answers go DIRECTLY to this source —
no shadow mode — with local tables as the error fallback only.

Error contract mirrors cr_api.py: every public helper returns the parsed
tool body on success and None on ANY failure (network, HTTP, tool error,
malformed body). Callers branch on None and fall back to local data —
nothing here raises across the module boundary. Tool errors are logged
loudly so failures are visible in #elixir-log triage; the served contract
version is logged as information, never as drift (see _note_contract).
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
# No contract pin. The hub's rule since 2026-09-25 (elixir-mcp
# docs/DECISIONS.md, "MCP majors track domain shifts, not agent-visible wire
# cleanup"): removing an unreliable response field is a PATCH, and a major
# is reserved for a domain-model change. So the version number cannot tell
# this program whether a field it reads survived. A major pin would miss
# exactly that case while warning on every major that changed nothing we
# read: pinned at 6, every process opened with a "contract drift" warning
# once the hub moved past 6.x (server 8.1.0 on 2026-09-25).
#
# The check that tracks the wire is in the readers instead. Every builder
# in capabilities/mcp_stats.py treats an absent or null field as
# UNAVAILABLE, never as 0: a count an answer depends on makes the builder
# return None, so the caller falls back to local data (or says the answer
# is unavailable where there is no local copy). The served version is
# still logged, as information.
_TIMEOUT_S = 15

_id_lock = threading.Lock()
_next_id = 0
_contract_seen: str | None = None


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
        # Since contract 3.18.0 the error carries a class; `retry` means the
        # call was fine and the answer is not in hand yet (a queued live
        # read, a cancelled analytical read), so it is a line, not a warning.
        err_class = err.get("class")
        (log.info if err_class == "retry" else log.warning)(
            "elixir-mcp: %s tool error %s%s: %s %s",
            name,
            err.get("code"),
            f" [{err_class}]" if err_class else "",
            err.get("message"),
            _describe_args(arguments),
        )
        return None
    _note_contract(body)
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


def _note_contract(body: dict) -> None:
    """Log the served contract version once per distinct value, at INFO.

    Information only: the first answer after boot, and again when the hub
    deploys a new contract while the bot runs. No version is treated as
    drift (see the note above MCP_URL)."""
    global _contract_seen
    version = (body.get("meta") or {}).get("contract_version") or None
    if version and version != _contract_seen:
        _contract_seen = version
        log.info("elixir-mcp: server contract %s", version)
