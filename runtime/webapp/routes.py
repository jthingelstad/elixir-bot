"""Observatory routes. Server-rendered Jinja2 pages (GET) + the three safe
ops (POST, same-origin-checked) + the chat panel endpoints."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from runtime.webapp import chat, ops, queries, ticks
from runtime.webapp.render import render

log = logging.getLogger("elixir.webapp")

IDENTITY_HEADER = "Tailscale-User-Login"


def _login(request: web.Request) -> str:
    return request.headers.get(IDENTITY_HEADER, "")


def _same_origin(request: web.Request) -> bool:
    """Reject cross-origin POSTs. No cookie/session to anchor a CSRF token on
    (the gate is the tailnet + identity header), so an Origin/Referer
    allowlist closes the residual vector. Same-origin form posts (and
    non-browser clients) typically omit Origin → allowed."""
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return True
    return origin.startswith(f"https://{request.host}") or origin.startswith(
        f"http://{request.host}"
    )


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


# ------------------------------------------------------------------ pages

async def overview(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.overview)
    live = ticks.recent_ticks(1)
    if live:
        data["latest_tick"] = live[0]
    scheduler_jobs = _scheduler_next_runs()
    flash = request.query.get("ok", "")
    return render("overview.html", request, nav="overview", flash=flash,
                  scheduler_jobs=scheduler_jobs, **data)


def _scheduler_next_runs() -> list[dict]:
    try:
        from runtime.helpers._common import _job_next_runs

        return _job_next_runs()
    except Exception:
        return []


async def ticks_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.ticks_page)
    history = ticks.recent_ticks(100)
    # Union of counter keys across history → stable column set for the table.
    keys: list[str] = []
    for t in history:
        for k in t:
            if k not in keys and k != "recorded_at":
                keys.append(k)
    return render("ticks.html", request, nav="ticks", history=history,
                  counter_keys=keys, **data)


async def members_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.members_page)
    return render("members.html", request, nav="members", **data)


async def awards_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.awards_page)
    return render("awards.html", request, nav="awards", **data)


async def baseline_page(request: web.Request) -> web.Response:
    # Query-param form (tags contain '#', which fights path segments).
    kind = request.query.get("kind", "")
    tag = request.query.get("tag", "")
    aspect = request.query.get("aspect", "")
    if not (kind and tag and aspect):
        raise web.HTTPBadRequest(text="kind, tag and aspect are required")
    row = await asyncio.to_thread(queries.baseline_detail, kind, tag, aspect)
    if row is None:
        raise web.HTTPNotFound(text="no such baseline")
    return render("baseline.html", request, nav="streams", row=row)


async def streams_page(request: web.Request) -> web.Response:
    stream = request.query.get("stream") or None
    event_type = request.query.get("event_type") or None
    data = await asyncio.to_thread(queries.streams_page, stream, event_type)
    return render("streams.html", request, nav="streams", **data)


async def raw_payload_page(request: web.Request) -> web.Response:
    try:
        payload_id = int(request.match_info["payload_id"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad payload id")
    row = await asyncio.to_thread(queries.raw_payload, payload_id)
    if row is None:
        raise web.HTTPNotFound(text="no such payload")
    return render("raw.html", request, nav="streams", row=row)


async def recognition_page(request: web.Request) -> web.Response:
    stream = request.query.get("stream") or None
    suppressed = request.query.get("suppressed") == "1"
    q = request.query.get("q") or None
    data = await asyncio.to_thread(queries.recognition_page, stream, suppressed, q)
    flash = request.query.get("ok", "")
    return render("recognition.html", request, nav="recognition", flash=flash, **data)


async def recognition_detail(request: web.Request) -> web.Response:
    key = request.match_info["key"]
    data = await asyncio.to_thread(queries.recognition_detail, key)
    if data is None:
        raise web.HTTPNotFound(text="no such recognition key")
    return render("recognition_detail.html", request, nav="recognition", key=key, **data)


async def member_page(request: web.Request) -> web.Response:
    tag = request.match_info["tag"]
    if not tag.startswith("#"):
        tag = f"#{tag}"
    data = await asyncio.to_thread(queries.member_page, tag.upper())
    if data is None:
        raise web.HTTPNotFound(text="no such player")
    return render("member.html", request, nav="", tag=tag.upper(), **data)


async def polling_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.polling_page)
    return render("polling.html", request, nav="polling", **data)


async def management_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.management_page)
    dryrun = request.query.get("dryrun", "")
    return render("management.html", request, nav="management", dryrun=dryrun, **data)


async def war_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.war_page)
    return render("war.html", request, nav="war", **data)


async def llm_page(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(queries.llm_page)
    return render("llm.html", request, nav="llm", **data)


async def chat_page(request: web.Request) -> web.Response:
    return render("chat.html", request, nav="chat")


# ------------------------------------------------------------------- chat

async def chat_get(request: web.Request) -> web.Response:
    msgs = await asyncio.to_thread(chat.list_messages, 50)
    return web.json_response({"messages": msgs})


async def chat_post(request: web.Request) -> web.Response:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="bad origin")
    data = await request.post()
    message = (data.get("message") or "").strip()
    if not message:
        raise web.HTTPBadRequest(text="missing message")
    await chat.post_message(message, _login(request))
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- safe ops

async def ops_tick(request: web.Request) -> web.Response:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="bad origin")
    try:
        job_id = await asyncio.to_thread(ops.schedule_tick_now)
        note = f"tick scheduled ({job_id})"
    except Exception as exc:
        log.exception("ops_tick failed")
        note = f"tick scheduling failed: {exc}"
    raise web.HTTPFound(f"/?ok={note}")


async def ops_intent_retry(request: web.Request) -> web.Response:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="bad origin")
    try:
        intent_id = int(request.match_info["intent_id"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="bad intent id")
    ok = await asyncio.to_thread(ops.retry_intent, intent_id)
    note = f"intent {intent_id} reset to pending" if ok else f"intent {intent_id} not retryable"
    raise web.HTTPFound(f"/recognition?ok={note}")


async def ops_review_dryrun(request: web.Request) -> web.Response:
    if not _same_origin(request):
        raise web.HTTPForbidden(text="bad origin")
    try:
        result = await asyncio.to_thread(ops.weekly_review_dryrun)
        promote = len(result.get("promote_eligible") or [])
        demote = len(result.get("demote_eligible") or [])
        note = (f"dry-run for {result.get('week_anchor')}: "
                f"{promote} promote-eligible, {demote} demote-eligible (rolled back)")
    except Exception as exc:
        log.exception("weekly review dry-run failed")
        note = f"dry-run failed: {exc}"
    raise web.HTTPFound(f"/management?dryrun={note}")


def add_routes(app: web.Application) -> None:
    app.add_routes([
        web.get("/healthz", healthz),
        web.get("/", overview),
        web.get("/members", members_page),
        web.get("/awards", awards_page),
        web.get("/baseline", baseline_page),
        web.get("/ticks", ticks_page),
        web.get("/streams", streams_page),
        web.get("/raw/{payload_id}", raw_payload_page),
        web.get("/recognition", recognition_page),
        web.get("/recognition/{key:.+}", recognition_detail),
        web.get("/member/{tag}", member_page),
        web.get("/polling", polling_page),
        web.get("/management", management_page),
        web.get("/war", war_page),
        web.get("/llm", llm_page),
        web.get("/chat", chat_page),
        web.get("/chat/messages", chat_get),
        web.post("/chat", chat_post),
        web.post("/ops/tick", ops_tick),
        web.post("/ops/intent/{intent_id}/retry", ops_intent_retry),
        web.post("/ops/weekly-review/dryrun", ops_review_dryrun),
    ])
