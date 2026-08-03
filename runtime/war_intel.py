"""The Clan Wars Intel Report — scouting the river race, delivered by email.

Runs once per war season (monthly-ish), on the training day before the first
battle day, which is the only moment the report is actionable: everyone's race
fame is still zero and there is still time to plan.

**Facts here, prose from the model.** `build_intel_context` assembles every
number and every named player deterministically from the CR API; the workflow
that follows is handed those facts and writes only the assessment prose. It has
no tools and cannot look anything up. That split is deliberate — the earlier
Discord version gave the model `cr_api` and asked it to compose the numbers
itself, and a scouting report is precisely where an invented player name or
trophy count does the most damage. A wrong adjective is a bad sentence; a wrong
roster is a bad decision.

Top five is by trophies, not by race fame: on the training day this runs, fame
is 0 for every player in every clan, so fame would rank nothing. Trophies say
who you will actually face; `last_seen` says whether they still play.

**Donations are deliberately absent.** The API's per-member `donations` and the
clan's `donationsPerWeek` are both WEEK-TO-DATE against a counter that resets
Monday ~00:10 UTC — and this report runs on the training day, which is that
Monday. Every clan read as "0 donations" in the first draft, which is not a fact
about them, it is a fact about when we looked. Anything reset-scoped is a trap
for a report pinned to the start of a cycle.

What replaced it is `race_form`: how the clan actually PLACED in its recent
river races, from the river race log, with the war-trophy change each week. That
is the most predictive thing available before a race starts — a clan on 1st,
1st, 2nd is a different problem from the same roster on 2nd, 5th, 5th, and the
raw roster stats cannot tell those apart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import cr_api
from engine.normalize import parse_cr_time

log = logging.getLogger("elixir")

TOP_MEMBERS = 5


def _age_days(last_seen: str | None, now: datetime) -> float | None:
    parsed = parse_cr_time(last_seen) if last_seen else None
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 86400.0)


def _activity_label(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 7:
        return f"{int(days)}d ago"
    if days < 30:
        return f"{int(days // 7)}w ago"
    return "30d+"


def _top_members(member_list: list[dict], now: datetime) -> list[dict]:
    ranked = sorted(member_list or [], key=lambda m: -(m.get("trophies") or 0))
    out = []
    for m in ranked[:TOP_MEMBERS]:
        days = _age_days(m.get("lastSeen"), now)
        out.append(
            {
                "name": m.get("name") or "(unnamed)",
                "tag": m.get("tag"),
                "trophies": m.get("trophies") or 0,
                "role": (m.get("role") or "member"),
                "last_seen_days": days,
                "last_seen_label": _activity_label(days),
            }
        )
    return out


def _roster_health(member_list: list[dict], now: datetime) -> dict:
    """How alive is this clan? Counted over the WHOLE roster, not the top five —
    a clan can have a fearsome top and nobody else logging in."""
    total = len(member_list or [])
    if not total:
        return {"total": 0, "active_7d": 0, "idle_30d": 0, "avg_trophies": 0}
    active = idle = 0
    for m in member_list:
        days = _age_days(m.get("lastSeen"), now)
        if days is None:
            continue
        if days < 7:
            active += 1
        if days >= 30:
            idle += 1
    trophies = [m.get("trophies") or 0 for m in member_list]
    return {
        "total": total,
        "active_7d": active,
        "idle_30d": idle,
        "avg_trophies": round(sum(trophies) / len(trophies)),
    }


def _race_form(tag: str, limit: int = 6) -> list[dict]:
    """Recent river race results for one clan, newest first.

    `rank` and `trophyChange` are the trustworthy fields here. The log's `fame`
    values are inconsistent across entries (and its per-participant fame is
    mostly zero even for clans that clearly played), so neither is carried —
    where a clan FINISHED is the signal, and it needs no interpretation.
    """
    try:
        log_payload = cr_api.get_river_race_log(tag=tag) or {}
    except Exception as exc:  # noqa: BLE001 - form is a bonus, never fatal
        log.warning("war intel: race log fetch failed for %s: %s", tag, exc)
        return []
    bare = tag.lstrip("#").upper()
    out = []
    for item in (log_payload.get("items") or [])[:limit]:
        for standing in item.get("standings") or []:
            clan = standing.get("clan") or {}
            if (clan.get("tag") or "").lstrip("#").upper() != bare:
                continue
            out.append(
                {
                    "season_id": item.get("seasonId"),
                    "section_index": item.get("sectionIndex"),
                    "rank": standing.get("rank"),
                    "trophy_change": standing.get("trophyChange"),
                }
            )
    return out


def _form_summary(form: list[dict]) -> dict:
    ranks = [f["rank"] for f in form if isinstance(f.get("rank"), int)]
    changes = [f["trophy_change"] for f in form if isinstance(f.get("trophy_change"), int)]
    if not ranks:
        return {}
    return {
        "races": len(ranks),
        "ranks": ranks,
        "avg_rank": round(sum(ranks) / len(ranks), 1),
        "wins": sum(1 for r in ranks if r == 1),
        "trophy_change": sum(changes) if changes else 0,
    }


def build_intel_context(*, season_id: int | None = None, now: datetime | None = None) -> dict:
    """Assemble the scouting facts for the current river race.

    Raises ValueError when there is no race or no opponent to scout — the caller
    decides whether that is a skip or a failure.
    """
    now = now or datetime.now(timezone.utc)
    war = cr_api.get_current_war()
    if not war:
        raise ValueError("no current river race")

    our_tag = f"#{cr_api.CLAN_TAG.lstrip('#').upper()}"
    clans = war.get("clans") or []
    if not clans:
        raise ValueError("river race has no clans")

    opponents = []
    for clan in clans:
        tag = f"#{(clan.get('tag') or '').lstrip('#').upper()}"
        if not tag or tag == our_tag:
            continue
        try:
            profile = cr_api.get_clan_by_tag(tag) or {}
        except Exception as exc:  # noqa: BLE001 - one bad opponent must not sink the report
            log.warning("war intel: profile fetch failed for %s: %s", tag, exc)
            opponents.append(
                {
                    "tag": tag,
                    "name": clan.get("name") or tag,
                    "profile_available": False,
                    "top_members": [],
                }
            )
            continue
        members = profile.get("memberList") or []
        location = (profile.get("location") or {}).get("name")
        opponents.append(
            {
                "tag": tag,
                "name": profile.get("name") or clan.get("name") or tag,
                "profile_available": True,
                "war_trophies": profile.get("clanWarTrophies"),
                "clan_score": profile.get("clanScore"),
                "required_trophies": profile.get("requiredTrophies"),
                "members": profile.get("members") or len(members),
                "clan_type": profile.get("type"),
                "location": location,
                "description": (profile.get("description") or "").strip() or None,
                "roster": _roster_health(members, now),
                "top_members": _top_members(members, now),
                "form": _form_summary(_race_form(tag)),
            }
        )

    if not opponents:
        raise ValueError("river race has no opponents besides us")

    # Rank by war trophies — the game's own measure of war strength, and the one
    # ordering we do not have to invent. The model rates threat separately.
    opponents.sort(key=lambda o: -(o.get("war_trophies") or 0))

    ours = next((c for c in clans if f"#{(c.get('tag') or '').lstrip('#').upper()}" == our_tag), {})
    # Our own numbers belong in the brief. A scouting report is comparative by
    # nature, and without them the model reaches for a comparison it cannot make:
    # the first draft asserted an opponent was "one war trophy below ours" when it
    # had never been told ours. Ground the comparison rather than forbid it.
    us = {}
    try:
        our_profile = cr_api.get_clan() or {}
        our_members = our_profile.get("memberList") or []
        us = {
            "war_trophies": our_profile.get("clanWarTrophies"),
            "clan_score": our_profile.get("clanScore"),
            "members": our_profile.get("members") or len(our_members),
            "required_trophies": our_profile.get("requiredTrophies"),
            "roster": _roster_health(our_members, now),
            "form": _form_summary(_race_form(our_tag)),
        }
    except Exception as exc:  # noqa: BLE001 - opponents are the point; ours is context
        log.warning("war intel: own-clan profile fetch failed: %s", exc)

    return {
        "season_id": season_id,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "our_tag": our_tag,
        "our_name": ours.get("name") or "POAP KINGS",
        "us": us,
        "period_type": war.get("periodType"),
        "section_index": war.get("sectionIndex"),
        "opponents": opponents,
    }


def facts_for_model(ctx: dict) -> str:
    """The brief the narrative workflow sees. Plain text, every number already
    resolved — the model's job is judgement, not arithmetic."""
    lines = [
        f"Our clan: {ctx['our_name']} ({ctx['our_tag']})",
        f"Season {ctx.get('season_id') or 'unknown'}, "
        f"race phase: {ctx.get('period_type') or 'unknown'}",
        f"Opponents this race: {len(ctx['opponents'])}",
    ]
    fmt = lambda f: (  # noqa: E731
        f"recent races (newest first): {'-'.join(str(r) for r in f['ranks'])}"
        f" | avg finish {f['avg_rank']}, {f['wins']} win(s),"
        f" net {f['trophy_change']:+d} war trophies over {f['races']} races"
    )
    us = ctx.get("us") or {}
    if us:
        roster = us.get("roster") or {}
        lines.append(
            f"US: war trophies {us.get('war_trophies')}, clan score {us.get('clan_score')}, "
            f"{us.get('members')} members, avg {roster.get('avg_trophies')} trophies, "
            f"{roster.get('active_7d')}/{roster.get('total')} played in the last 7 days. "
            "Compare opponents to these numbers, never to remembered ones."
        )
        if us.get("form"):
            lines.append(f"US {fmt(us['form'])}")
    lines.append("")
    for o in ctx["opponents"]:
        if not o.get("profile_available"):
            lines.append(
                f"### {o['name']} ({o['tag']}) — PROFILE UNAVAILABLE, keep this entry brief"
            )
            lines.append("")
            continue
        roster = o.get("roster") or {}
        lines.append(f"### {o['name']} ({o['tag']})")
        lines.append(
            f"war trophies {o.get('war_trophies')}, clan score {o.get('clan_score')}, "
            f"{o.get('members')} members, entry requirement {o.get('required_trophies')} trophies, "
            f"type {o.get('clan_type')}"
            + (f", based in {o['location']}" if o.get("location") else "")
        )
        lines.append(
            f"roster: avg {roster.get('avg_trophies')} trophies, "
            f"{roster.get('active_7d')}/{roster.get('total')} played in the last 7 days, "
            f"{roster.get('idle_30d')} idle 30d+"
        )
        if o.get("form"):
            lines.append(fmt(o["form"]))
        for m in o["top_members"]:
            lines.append(
                f"  - {m['name']}: {m['trophies']} trophies, {m['role']}, "
                f"last seen {m['last_seen_label']}"
            )
        lines.append("")
    return "\n".join(lines)


_ROLE_LABEL = {
    "leader": "Leader",
    "coLeader": "Co-leader",
    "elder": "Elder",
    "member": "Member",
}


def _member_table(top: list[dict]) -> list[str]:
    rows = [
        "| Player | Trophies | Role | Last seen |",
        "| --- | ---: | --- | --- |",
    ]
    for m in top:
        rows.append(
            f"| {m['name']} | {m['trophies']:,} | {_ROLE_LABEL.get(m['role'], m['role'])} "
            f"| {m['last_seen_label']} |"
        )
    return rows


def render_war_intel_email(ctx: dict, narrative: dict | None = None) -> tuple[str, str]:
    """Assemble the email. Every number and name below comes from ``ctx``; the
    model supplies only the assessment and the per-clan paragraphs. Returns
    (subject, markdown) — `outbound.send` renders the markdown to styled HTML.
    """
    nar = narrative if isinstance(narrative, dict) else {}
    by_tag = {}
    for entry in nar.get("clans") or []:
        if isinstance(entry, dict) and entry.get("tag"):
            by_tag[f"#{str(entry['tag']).lstrip('#').upper()}"] = entry

    season = ctx.get("season_id")
    subject = (
        f"POAP KINGS — Clan Wars Intel, Season {season}"
        if season
        else "POAP KINGS — Clan Wars Intel"
    )

    parts: list[str] = []
    opener = nar.get("assessment")
    if opener:
        parts.append(str(opener))
    else:
        parts.append(
            f"A new river race is underway. Here is what we know about the "
            f"{len(ctx['opponents'])} clans standing between us and the top of the board."
        )

    # Threat order comes from the model when it supplies one, since threat is a
    # judgement; war trophies are the fallback ordering and already applied.
    ordered = ctx["opponents"]
    if by_tag:
        ordered = sorted(
            ctx["opponents"],
            key=lambda o: -(by_tag.get(o["tag"], {}).get("threat") or 0),
        )

    for o in ordered:
        entry = by_tag.get(o["tag"], {})
        threat = entry.get("threat")
        heading = f"## {o['name']} ({o['tag']})"
        if isinstance(threat, int) and 1 <= threat <= 5:
            heading += f" — Threat {threat}/5"
        parts.append(heading)

        if not o.get("profile_available"):
            parts.append("_Clan profile unavailable from the API this run — no roster to scout._")
            continue

        roster = o.get("roster") or {}
        stat_line = (
            f"**{o.get('war_trophies'):,} war trophies** · {o.get('members')} members · "
            f"avg {roster.get('avg_trophies'):,} trophies · "
            f"{roster.get('active_7d')}/{roster.get('total')} active this week"
        )
        if o.get("required_trophies"):
            stat_line += f" · entry {o['required_trophies']:,}"
        if o.get("location"):
            stat_line += f" · {o['location']}"
        parts.append(stat_line)

        form = o.get("form") or {}
        if form.get("ranks"):
            parts.append(
                f"**Recent river races** (newest first): "
                f"{' · '.join(str(r) for r in form['ranks'])} — "
                f"avg finish {form['avg_rank']}, {form['trophy_change']:+d} war trophies"
            )

        paragraph = entry.get("paragraph")
        if paragraph:
            parts.append(str(paragraph))

        if o["top_members"]:
            parts.append("**Top 5 by trophies**")
            parts.append("\n".join(_member_table(o["top_members"])))

    closer = nar.get("closer")
    parts.append(str(closer) if closer else "Scout well. Win the river. — E")
    return subject, "\n\n".join(parts)


__all__ = [
    "build_intel_context",
    "facts_for_model",
    "render_war_intel_email",
]
