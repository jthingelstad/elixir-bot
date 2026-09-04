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
# The contract generation this integration was built against. A different
# MAJOR.MINOR is worth a loud log line (the server renames freely in
# alpha); calls still proceed — the fallback path covers real breakage.
PINNED_CONTRACT = "0.10"
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
        content = envelope["result"]["content"][0]["text"]
        body = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("elixir-mcp: %s malformed response: %s", name, exc)
        return None
    if envelope["result"].get("isError"):
        err = body.get("error", {})
        log.warning(
            "elixir-mcp: %s tool error %s: %s",
            name,
            err.get("code"),
            err.get("message"),
        )
        return None
    _check_contract(body)
    return body


def _check_contract(body: dict) -> None:
    global _contract_warned
    if _contract_warned:
        return
    version = (body.get("meta") or {}).get("contract_version") or ""
    if version and not version.startswith(PINNED_CONTRACT + "."):
        _contract_warned = True
        log.warning(
            "elixir-mcp: contract drift — server %s, integration built for %s.x; "
            "review the tool surface",
            version,
            PINNED_CONTRACT,
        )
