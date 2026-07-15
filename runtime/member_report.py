"""The Arena Dispatch — the personalized weekly member report.

Two halves, kept apart on purpose (facts are rendered, voice is generated):
  * build_member_report_context() gathers a member's week as pure facts.
  * render_member_report() turns facts + the LLM narrative into the email body.

Every number here is computed from the data; the model only narrates the facts it
is handed (see agent/prompt_builders._member_report_system). Nothing in this module
calls the LLM — the job wires generation in between build and render.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from capabilities import members as member_capability
from engine.normalize import humanize_badge, humanize_game_mode
from engine.profiles import MODE_DISPLAY, playstyle_line
from storage import game_events
from storage._formatting import preferred_display_name

_ISO_CUTOFF = "%Y-%m-%dT%H:%M:%S"

_CARD_EVENT_TYPES = ("card_unlocked", "card_level_milestone", "collection_level_milestone")


def _anchor(now: str | None) -> datetime:
    return (datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)
            if now else datetime.now(timezone.utc))


def _cutoff(days: int, now: str | None = None) -> str:
    """ISO cutoff for *_events.observed_at columns (stored ISO)."""
    return (_anchor(now) - timedelta(days=days)).strftime(_ISO_CUTOFF)


def _cutoff_compact(days: int, now: str | None = None) -> str:
    """Compact cutoff for battle_events.battle_time, which is CR-compact
    (`20260701T135733.000Z`) — comparing it against an ISO cutoff silently matches
    everything, so battle windows MUST use this form."""
    return (_anchor(now) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S")


def _window_battles(conn, tag: str, cutoff: str, until: str | None = None) -> list[dict]:
    where = "player_tag = ? AND battle_time >= ?"
    params: list = [tag, cutoff]
    if until:
        where += " AND battle_time < ?"
        params.append(until)
    rows = conn.execute(
        f"SELECT battle_time, game_mode_name, mode_group, outcome, crowns_for, crowns_against, "
        f"trophy_change, is_war, is_ranked, is_special_event, teammate_tag "
        f"FROM battle_events WHERE {where} ORDER BY battle_time DESC",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def _battle_tally(battles: list[dict]) -> dict:
    wins = sum(1 for b in battles if b["outcome"] == "W")
    losses = sum(1 for b in battles if b["outcome"] == "L")
    draws = sum(1 for b in battles if b["outcome"] not in ("W", "L"))
    net_trophies = sum(int(b["trophy_change"] or 0) for b in battles)
    by_mode: dict[str, dict] = {}
    for b in battles:
        mg = b.get("mode_group") or "other"
        slot = by_mode.setdefault(mg, {"battles": 0, "wins": 0})
        slot["battles"] += 1
        slot["wins"] += 1 if b["outcome"] == "W" else 0
    return {
        "battles": len(battles), "wins": wins, "losses": losses, "draws": draws,
        "win_rate": round(wins / len(battles), 3) if battles else 0.0,
        "net_trophies": net_trophies,
        "by_mode": dict(sorted(by_mode.items(), key=lambda kv: -kv[1]["battles"])),
    }


def _battle_of_week(battles: list[dict]) -> dict | None:
    """The week's standout fight: biggest crown margin, then trophy swing, wins first."""
    if not battles:
        return None
    def score(b):
        margin = int(b["crowns_for"] or 0) - int(b["crowns_against"] or 0)
        return (1 if b["outcome"] == "W" else 0, margin, int(b["trophy_change"] or 0),
                int(b["crowns_for"] or 0))
    best = max(battles, key=score)
    return {
        "battle_time": best["battle_time"],
        "mode": humanize_game_mode(best["game_mode_name"]) or best["game_mode_name"] or "a battle",
        "outcome": best["outcome"],
        "crowns_for": best["crowns_for"], "crowns_against": best["crowns_against"],
        "trophy_change": best["trophy_change"],
    }


def _mode_label(mode_group: str) -> str:
    return MODE_DISPLAY.get(mode_group, (mode_group or "other").replace("_", " "))


def _battles_rank(conn, tag: str, cutoff: str) -> dict | None:
    """The member's rank among current clanmates by battles played this week."""
    rows = conn.execute(
        "SELECT b.player_tag, COUNT(*) AS n FROM battle_events b "
        "JOIN clan_memberships cm ON cm.player_tag = b.player_tag AND cm.left_at IS NULL "
        "WHERE b.battle_time >= ? GROUP BY b.player_tag ORDER BY n DESC",
        (cutoff,),
    ).fetchall()
    for i, r in enumerate(rows, start=1):
        if r["player_tag"] == tag:
            return {"rank": i, "of": len(rows), "battles": r["n"]}
    return None


def _clan_trending_cards(conn, cutoff: str, *, min_members: int = 2, limit: int = 6) -> list[dict]:
    """Cards freshly unlocked across CURRENT clan members this week — the real
    'what's new in the meta' signal (the game-stream catalog bootstrapped every
    card at once, so a fresh clan-wide unlock wave is the truthful new-card cue —
    e.g. Ronin, unlocked by a dozen members the week it landed)."""
    rows = conn.execute(
        "SELECT json_extract(pe.payload_json,'$.card_name') AS card, "
        "COUNT(DISTINCT pe.player_tag) AS members "
        "FROM player_events pe "
        "JOIN clan_memberships cm ON cm.player_tag = pe.player_tag AND cm.left_at IS NULL "
        "WHERE pe.event_type = 'card_unlocked' AND pe.observed_at >= ? "
        "GROUP BY card HAVING members >= ? ORDER BY members DESC LIMIT ?",
        (cutoff, min_members, limit),
    ).fetchall()
    return [{"card_name": r["card"], "members": r["members"]} for r in rows if r["card"]]


def build_member_report_context(tag: str, name: str, *, days: int = 7,
                                now: str | None = None) -> dict:
    """Gather one member's week as pure facts — the input to both the renderer and
    the (grounded) narrative model."""
    conn = db.get_connection()
    try:
        display = preferred_display_name(conn, tag, name)
        cutoff = _cutoff(days, now)              # ISO — for *_events.observed_at
        cutoff_c = _cutoff_compact(days, now)    # compact — for battle_events.battle_time
        prev_c = _cutoff_compact(days * 2, now)

        member_read = member_capability.get_member_intelligence(
            tag,
            facets=("profile", "playstyle", "war"),
            days=28,
            conn=conn,
        )
        profile = member_read.get("profile") or {}
        mode_profile = member_read.get("playstyle") or {}

        battles = _window_battles(conn, tag, cutoff_c)
        tally = _battle_tally(battles)
        prior = _battle_tally(_window_battles(conn, tag, prev_c, until=cutoff_c))
        botw = _battle_of_week(battles)

        events = db.list_recent_events(days=days, subject_key=tag, limit=200, conn=conn)
        badges, cards, ranked, other = [], [], [], []
        for e in events:
            et, payload = e.get("event_type"), e.get("payload") or {}
            if et == "badge_earned":
                badges.append({
                    "label": humanize_badge(payload.get("badge_name") or ""),
                    "level": payload.get("level"),
                    "observed_at": e.get("observed_at"),
                    "image_url": payload.get("image_url"),
                })
            elif et in _CARD_EVENT_TYPES:
                cards.append({
                    "type": et, "card_name": payload.get("card_name"),
                    "rarity": payload.get("rarity"),
                    "milestone": payload.get("milestone") or payload.get("collection_level"),
                    "observed_at": e.get("observed_at"),
                })
            elif et and et.startswith("pol_") or et in ("ultimate_champion_reached",):
                ranked.append({"event_type": et, "payload": payload,
                               "observed_at": e.get("observed_at")})
            elif et in ("best_trophies_peak",):
                other.append({"event_type": et, "payload": payload,
                              "observed_at": e.get("observed_at")})

        war = member_read.get("war")

        stream = game_events.recent_game_events(conn, days=days, now=now)
        new_cards = [s["payload"] for s in stream if s["event_type"] == "card_added"]
        new_events = [s["payload"] for s in stream if s["event_type"] == "event_started"]

        return {
            "tag": tag,
            "name": display,
            "days": days,
            "window": {"from": cutoff, "generated_at": (now or datetime.now(timezone.utc)
                                                        .strftime("%Y-%m-%dT%H:%M:%SZ"))},
            "profile": {
                "trophies": profile.get("trophies"),
                "best_trophies": profile.get("best_trophies"),
                "role": profile.get("role"),
                "clan_rank": profile.get("clan_rank"),
                "arena": profile.get("arena_name"),
                "highlight": profile.get("profile_highlight"),
                "collection_level": profile.get("cr_collection_level"),
                "donations_week": profile.get("donations_week"),
                "signature_cards": _sig_names(profile.get("signature_cards")),
            },
            "playstyle": {
                "identity": mode_profile.get("identity"),
                "secondary": mode_profile.get("secondary"),
                "line": playstyle_line(mode_profile),
                "modes": mode_profile.get("modes"),
                "duo_partners": mode_profile.get("duo_partners"),
            },
            "battles": {
                "tally": tally,
                "prior_tally": prior,
                "battle_of_week": botw,
                "log": battles,
            },
            "badges": badges,
            "cards": cards,
            "ranked": ranked,
            "milestones": other,
            "war": war,
            "clan_standing": _battles_rank(conn, tag, cutoff_c),
            "game_stream": {
                "new_cards": new_cards,
                "new_events": new_events,
                "trending_cards": _clan_trending_cards(conn, cutoff),
            },
        }
    finally:
        conn.close()


def _sig_names(sig) -> list[str]:
    if isinstance(sig, dict):
        sig = sig.get("cards") or []
    return [c.get("name") for c in (sig or []) if isinstance(c, dict) and c.get("name")][:6]


def _achievements(ctx: dict) -> list[str]:
    """Notable, grounded accomplishments worth celebrating — plain phrases the
    narrative can lift. Derived only from real numbers."""
    t = ctx["battles"]["tally"]
    out: list[str] = []
    if t["battles"] >= 10 and t["win_rate"] >= 0.6:
        out.append(f"a sharp {_pct(t['win_rate'])}% win rate")
    if t["net_trophies"] >= 100:
        out.append(f"climbed +{t['net_trophies']} trophies")
    streak = _win_streak(ctx["battles"]["log"])
    if streak >= 3:
        out.append(f"a {streak}-win streak")
    if any(m["event_type"] == "best_trophies_peak" for m in ctx.get("milestones") or []):
        out.append("a new personal-best trophy count")
    cs = ctx.get("clan_standing")
    if cs and cs["rank"] <= 5:
        out.append(f"top-{cs['rank']} in the clan for battles played")
    return out


def facts_for_model(ctx: dict) -> str:
    """A compact, numbers-only brief the narrative model may speak to — and only
    this. Keeps the voice grounded: no card/number/event appears here that isn't
    a real fact from the member's week. The model decides what leads and how it
    flows; these are just the facts it's allowed to use."""
    t = ctx["battles"]["tally"]
    p = ctx["playstyle"]
    prof = ctx["profile"]
    lines = [
        f"MEMBER: {ctx['name']}",
        f"WINDOW: last {ctx['days']} days",
        f"TROPHIES: {prof.get('trophies')} (best {prof.get('best_trophies')}), "
        f"net this week {t['net_trophies']:+d}"
        + (f", arena {prof.get('arena')}" if prof.get("arena") else ""),
        f"RECORD: {t['battles']} battles, {t['wins']}-{t['losses']} "
        f"({_pct(t['win_rate'])}% win rate)",
        f"PLAYSTYLE: {p.get('identity')}" + (f" — {p['line']}" if p.get("line") else ""),
        "MODES: " + (", ".join(f"{_mode_label(m)} {v['battles']}" for m, v in
                               (t["by_mode"] or {}).items()) or "none"),
    ]
    if prof.get("donations_week") is not None:
        lines.append(f"DONATIONS THIS WEEK: {prof['donations_week']} cards")
    if prof.get("collection_level"):
        lines.append(f"COLLECTION LEVEL: {prof['collection_level']}")
    ach = _achievements(ctx)
    if ach:
        lines.append("WORTH CELEBRATING: " + "; ".join(ach))
    if ctx["battles"]["battle_of_week"]:
        b = ctx["battles"]["battle_of_week"]
        lines.append(f"BATTLE OF THE WEEK: {b['outcome']} {b['crowns_for']}-{b['crowns_against']} "
                     f"in {b['mode']} (trophy {int(b['trophy_change'] or 0):+d})")
    if ctx["badges"]:
        lines.append("BADGES THIS WEEK: " + ", ".join(b["label"] for b in ctx["badges"]))
    if ctx["cards"]:
        lines.append("CARD ACTIVITY: " + ", ".join(
            f"{c['card_name']} ({c['type'].replace('_',' ')})" for c in ctx["cards"] if c.get("card_name")))
    if ctx["war"]:
        s = (ctx["war"] or {}).get("season") or {}
        lines.append(f"WAR: {s.get('total_decks_used', 0)} decks used, "
                     f"{s.get('total_points', 0)} points this season")
    if ctx["clan_standing"]:
        cs = ctx["clan_standing"]
        lines.append(f"CLAN STANDING: #{cs['rank']} of {cs['of']} clanmates by battles played")
    tc = ctx["game_stream"].get("trending_cards") or []
    if tc:
        lines.append("HOT NEW CARD IN THE CLAN THIS WEEK (fresh unlocks): "
                     + ", ".join(f"{c['card_name']} ({c['members']} members)" for c in tc))
    ne = ctx["game_stream"]["new_events"]
    if ne:
        lines.append("NEW EVENTS: " + ", ".join(e.get("title") or "" for e in ne))
    return "\n".join(lines)


# ── Rendering (facts → email markdown) ────────────────────────────────────────

def _fmt_dt(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%a %H:%M")
    except (ValueError, TypeError):
        return str(iso or "")[:16]


def _outcome_cell(o: str | None) -> str:
    return {"W": "✅ W", "L": "❌ L"}.get(o or "", "➖ D")


def _win_streak(log: list[dict]) -> int:
    """Longest run of consecutive wins in the week (log is newest-first)."""
    best = run = 0
    for b in reversed(log):
        run = run + 1 if b.get("outcome") == "W" else 0
        best = max(best, run)
    return best


def _pct(x: float) -> int:
    return round((x or 0) * 100)


def _fallback_overview(ctx: dict) -> str:
    t = ctx["battles"]["tally"]
    return (f"Here's your week: **{t['wins']}–{t['losses']}** across **{t['battles']}** battles, "
            f"a net of **{t['net_trophies']:+d}** trophies.")


def _fallback_standouts(ctx: dict) -> str:
    bits: list[str] = []
    b = ctx["battles"]["battle_of_week"]
    if b:
        bits.append(f"Your standout was a **{b['outcome']}** at {b['crowns_for']}–"
                    f"{b['crowns_against']} in {b['mode']}.")
    ach = _achievements(ctx)
    if ach:
        bits.append("Worth noting: " + ", ".join(ach) + ".")
    tc = ctx["game_stream"].get("trending_cards") or []
    if tc:
        bits.append(f"And **{tc[0]['card_name']}** is the clan's card of the week "
                    f"({tc[0]['members']} unlocks).")
    return " ".join(bits)


def render_member_report(ctx: dict, narrative: dict | None = None) -> tuple[str, str]:
    """Assemble the email. The deterministic layer owns only the scorecard and the
    full battle-log table; everything between is grounded narrative the model shapes
    around what actually mattered that week — no fixed section grid, no images, and
    no title (the subject line is the title). Returns (subject, markdown)."""
    nar = narrative or {}
    name = ctx["name"]
    t = ctx["battles"]["tally"]
    prof = ctx["profile"]

    parts: list[str] = []

    # Scorecard — the one stat block up top. No H1; the subject is the title.
    troph = prof.get("trophies")
    troph_str = f"{troph:,}" if isinstance(troph, int) else "—"
    parts.append(f"### 🏆 {troph_str}  ·  {t['wins']}–{t['losses']}  ·  "
                 f"{_pct(t['win_rate'])}% win  ·  {t['battles']} battles")
    sub = [f"Your week in the arena · **{t['net_trophies']:+d}** trophies"]
    if prof.get("best_trophies"):
        sub.append(f"best {prof['best_trophies']:,}")
    if prof.get("arena"):
        sub.append(prof["arena"])
    parts.append("_" + "  ·  ".join(sub) + "_")

    # Grounded narrative — the organic middle (model chooses what leads).
    parts.append(nar.get("overview") or _fallback_overview(ctx))
    standouts = nar.get("standouts") or _fallback_standouts(ctx)
    if standouts:
        parts.append(standouts)
    if nar.get("meta"):
        parts.append(nar["meta"])

    # The full tape — the one structured reference.
    log = ctx["battles"]["log"]
    if log:
        tape = ["## The full tape", "", "| When | Mode | Result | Crowns | 🏆 |",
                "|---|---|---|---|---|"]
        for b in log:
            mode = humanize_game_mode(b.get("game_mode_name")) or _mode_label(b.get("mode_group"))
            crowns = f"{b.get('crowns_for', 0)}–{b.get('crowns_against', 0)}"
            tc = b.get("trophy_change")
            tro = f"{int(tc):+d}" if tc is not None else ""
            tape.append(f"| {_fmt_dt(b.get('battle_time'))} | {mode} | "
                        f"{_outcome_cell(b.get('outcome'))} | {crowns} | {tro} |")
        parts.append("\n".join(tape))

    parts.append(nar.get("closer") or "Same time next week. Keep the crowns coming. — E")

    subject = f"{name} — your week in the arena 👑"
    return subject, "\n\n".join(parts)
