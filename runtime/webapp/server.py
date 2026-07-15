"""The Observatory server: an aiohttp app run in the Elixir bot process, bound
to loopback and exposed tailnet-only via `tailscale serve`. Auth = the
Tailscale identity header.

Security model (ported from studio-thing workshop_bot/webapp): the app binds
127.0.0.1 only, so the ONLY path to it is the Tailscale serve proxy, which
injects `Tailscale-User-Login` for the authenticated tailnet user. A direct
request to the loopback port carries no such header, so requiring it (and
matching the allowed login) rejects both non-tailnet access and any other
tailnet user. No tokens, no cookies.

Exposure (one-time): tailscale serve --bg --https=8444 http://127.0.0.1:8771
(workshop bot owns 8770/8443 on this Mac).
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from runtime.webapp.routes import add_routes

log = logging.getLogger("elixir.webapp")

# The header Tailscale `serve` injects for tailnet requests.
IDENTITY_HEADER = "Tailscale-User-Login"

# Typed app key for the bot handle (aiohttp wants AppKey, not a str key).
DEPS: web.AppKey = web.AppKey("deps", object)

_runner: web.AppRunner | None = None


def _port() -> int:
    return int(os.environ.get("ELIXIR_WEBAPP_PORT") or 8771)


def _allowed_login() -> str:
    # The tailnet is just Jamie; default to his login (same default as the
    # workshop webapp). Empty string = allow any authenticated tailnet user.
    return os.environ.get("TAILSCALE_ALLOWED_LOGIN", "jthingelstad@github")


@web.middleware
async def _identity_mw(request: web.Request, handler):
    if request.path == "/healthz":
        return await handler(request)  # local liveness check, no identity
    login = request.headers.get(IDENTITY_HEADER, "")
    allowed = _allowed_login()
    if not login or (allowed and login != allowed):
        return web.Response(
            status=403, text="Forbidden — Tailscale identity required.\n"
        )
    return await handler(request)


def build_app(deps=None) -> web.Application:
    """Build the app (used by start_webapp and by tests, which skip the port bind)."""
    app = web.Application(middlewares=[_identity_mw])
    app[DEPS] = deps
    add_routes(app)
    return app


async def start_webapp(deps=None) -> None:
    """Start the loopback aiohttp server (idempotent — on_ready re-fires on
    every Discord reconnect). ``deps`` carries the bot handle for handlers
    that need the live process (safe ops, chat)."""
    global _runner
    if _runner is not None:
        return
    app = build_app(deps=deps)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _port())
    await site.start()
    _runner = runner  # module-global keeps it alive
    log.info(
        "elixir webapp on 127.0.0.1:%d (tailnet-only via tailscale serve :8444; allowed=%s)",
        _port(),
        _allowed_login() or "(any tailnet user)",
    )
