"""Jinja2 rendering for the Observatory. Templates live in templates/;
autoescape on. `render(...)` injects the common context (the signed-in
Tailscale login) so routes pass only page-specific data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jinja2
from aiohttp import web


def _tojson_pretty(value) -> str:
    """Render a JSON blob (str or object) as pretty-printed JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _reltime(value) -> str:
    """ISO/CR-compact timestamp → '4m ago' (falls back to the raw string)."""
    if not value:
        return ""
    text = str(value)
    from engine.normalize import parse_cr_time  # the single parser

    dt = parse_cr_time(text)
    if dt is None:
        return text
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        seconds = -seconds
        suffix = "from now"
    else:
        suffix = "ago"
    if seconds < 90:
        return f"{int(seconds)}s {suffix}"
    if seconds < 5400:
        return f"{int(seconds // 60)}m {suffix}"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h {suffix}"
    return f"{int(seconds // 86400)}d {suffix}"


_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(Path(__file__).parent / "templates")),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["tojson_pretty"] = _tojson_pretty
_env.filters["reltime"] = _reltime


def render(
    template: str, request: web.Request, *, status: int = 200, **ctx
) -> web.Response:
    # Identity is validated by the middleware; read it back off the header.
    ctx.setdefault("login", request.headers.get("Tailscale-User-Login", ""))
    body = _env.get_template(template).render(**ctx)
    return web.Response(text=body, content_type="text/html", status=status)
