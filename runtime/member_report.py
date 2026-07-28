"""The Arena Dispatch — the personalized weekly member report.

Two halves, kept apart on purpose (facts are rendered, voice is generated):
  * build_member_report_context() gathers a member's week as pure facts.
  * render_member_report() turns facts + the LLM narrative into the email body.

Every number here is computed from the data; the model only narrates the facts it
is handed (see agent/prompt_builders._member_report_system). Nothing in this module
calls the LLM — the job wires generation in between build and render.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import db
from capabilities import members as member_capability
from engine.normalize import humanize_badge, humanize_game_mode
from engine.profiles import MODE_DISPLAY, playstyle_line
from storage import game_events
from storage._formatting import preferred_display_name
from storage.game_modes import mode_group_label
from storage.player import NEAR_MISS_TOWER_HP

_ISO_CUTOFF = "%Y-%m-%dT%H:%M:%S"

_CARD_EVENT_TYPES = (
    "card_unlocked",
    "card_level_milestone",
    "collection_level_milestone",
)


def _anchor(now: str | None) -> datetime:
    return (
        datetime.fromisoformat(str(now).replace("Z", "+00:00")).astimezone(timezone.utc)
        if now
        else datetime.now(timezone.utc)
    )


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
        f"trophy_change, is_war, is_ranked, is_special_event, teammate_tag, deck_json, "
        f"opponent_deck_json, opponent_name, opponent_clan_name, support_cards_json, "
        f"elixir_leaked, opponent_elixir_leaked, princess_towers_hp_json, "
        f"opponent_princess_towers_hp_json "
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
        "battles": len(battles),
        "wins": wins,
        "losses": losses,
        "draws": draws,
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
        return (
            1 if b["outcome"] == "W" else 0,
            margin,
            int(b["trophy_change"] or 0),
            int(b["crowns_for"] or 0),
        )

    best = max(battles, key=score)
    return {
        "battle_time": best["battle_time"],
        "mode": humanize_game_mode(best["game_mode_name"]) or best["game_mode_name"] or "a battle",
        "outcome": best["outcome"],
        "crowns_for": best["crowns_for"],
        "crowns_against": best["crowns_against"],
        "trophy_change": best["trophy_change"],
    }


def _mode_label(mode_group: str) -> str:
    return MODE_DISPLAY.get(mode_group, (mode_group or "other").replace("_", " "))


def _deck_card_names(deck_json) -> list[str]:
    """Card names from a battle's stored deck (`deck_json` is `[{id,name,level},…]`)."""
    if not deck_json:
        return []
    try:
        cards = json.loads(deck_json) if isinstance(deck_json, str) else deck_json
    except ValueError, TypeError:
        return []
    return [str(c["name"]) for c in (cards or []) if isinstance(c, dict) and c.get("name")]


def _deck_card_modes(deck_json) -> list[tuple[str, str | None]]:
    """(name, played_as) per card — an Evo Knight is not a plain Knight."""
    if not deck_json:
        return []
    try:
        cards = json.loads(deck_json) if isinstance(deck_json, str) else deck_json
    except ValueError, TypeError:
        return []
    out = []
    for card in cards or []:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        mode = card.get("evolution_level")
        out.append((str(card["name"]), "evo" if mode == 1 else "hero" if mode == 2 else None))
    return out


def _towers(towers_json) -> list[int]:
    """Surviving princess tower HP. The API omits destroyed towers rather than
    zeroing them, so length is the survivor count and NULL means both fell."""
    if not towers_json:
        return []
    try:
        towers = json.loads(towers_json) if isinstance(towers_json, str) else towers_json
    except ValueError, TypeError:
        return []
    return [t for t in (towers or []) if isinstance(t, int) and not isinstance(t, bool)]


_SPARK = "▁▂▃▄▅▆▇█"
_DEAD = "·"


def _spark_char(value: float, ceiling: float) -> str:
    """One block glyph for `value` on a 0..ceiling scale."""
    if ceiling <= 0:
        return _SPARK[0]
    idx = int(round((max(0.0, min(float(value), ceiling)) / ceiling) * (len(_SPARK) - 1)))
    return _SPARK[idx]


def _tower_spark(towers_json, full_hp: float) -> str:
    """Two glyphs: the health of the two princess towers, tallest = healthiest.

    A destroyed tower is OMITTED by the API rather than zeroed, so a short list
    means the missing ones fell -- rendered as `·`. Scanning a column of these
    shows at a glance which games went to the wire.
    """
    towers = sorted(_towers(towers_json), reverse=True)
    glyphs = [_spark_char(hp, full_hp) for hp in towers[:2]]
    return "".join(glyphs) + _DEAD * (2 - len(glyphs))


def _battle_scale(battles: list[dict]) -> dict:
    """Per-member reference points for the sparklines.

    Both scales are RELATIVE TO THIS MEMBER, because neither has a universal
    maximum. Tower HP scales with tower level, so their healthiest observed
    tower is their 100%.

    Elixir needed the same treatment: leaked runs median 3.3 but reaches 300 in
    long duels, so a fixed ceiling either flattens every normal battle into the
    bottom block or lets outliers own the scale. The member's own 90th
    percentile puts the spread where their games actually are and saturates
    about a tenth of them. Own and opponent share one ceiling so the two glyphs
    in a cell are directly comparable.
    """
    own = [hp for b in battles for hp in _towers(b.get("princess_towers_hp_json"))]
    opp = [hp for b in battles for hp in _towers(b.get("opponent_princess_towers_hp_json"))]
    leaked = sorted(
        float(v)
        for b in battles
        for v in (b.get("elixir_leaked"), b.get("opponent_elixir_leaked"))
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    )
    ceiling = leaked[int(len(leaked) * 0.9)] if leaked else 0.0
    return {
        "own_full_hp": max(own) if own else 0,
        "opponent_full_hp": max(opp) if opp else 0,
        # Floor keeps a very tidy week from rendering noise as a full bar.
        "elixir_ceiling": max(ceiling, 6.0),
    }


def _card_matchups(battles: list[dict], *, min_faced: int | None = None, limit: int = 4) -> dict:
    """Win/loss record against each card the member actually faced.

    The deepest thing the new battle record supports: not "you lost to Mega
    Knight" but "you are 1-8 against it". A card seen in both wins and losses
    is a matchup, and a matchup has a record. Counted once per battle so a duel
    (2-3 sub-decks under one row) cannot double-count a shared card.

    `min_faced` guards against reading a story into two games; every entry
    reports `faced` so the sample is never hidden.
    """
    # Scale the sample floor to how much the member actually played. A 0-4
    # record is a story in a 20-battle week and noise in a 300-battle one, and
    # this clan spans both in the same week.
    if min_faced is None:
        min_faced = max(4, len(battles) // 40)
    rec: dict[tuple[str, str | None], dict] = {}
    for b in battles:
        outcome = b.get("outcome")
        if outcome not in ("W", "L"):
            continue
        for key in set(_deck_card_modes(b.get("opponent_deck_json"))):
            slot = rec.setdefault(key, {"wins": 0, "losses": 0})
            slot["wins" if outcome == "W" else "losses"] += 1
    rows = []
    for (name, played_as), slot in rec.items():
        faced = slot["wins"] + slot["losses"]
        if faced < min_faced:
            continue
        rows.append(
            {
                "name": name,
                "played_as": played_as,
                "faced": faced,
                "wins": slot["wins"],
                "losses": slot["losses"],
                "win_rate": round(slot["wins"] / faced, 3),
            }
        )
    # Ties broken by sample size so the better-evidenced matchup leads.
    hardest = sorted(rows, key=lambda r: (r["win_rate"], -r["faced"]))
    return {
        "toughest": hardest[:limit],
        "best": list(reversed(hardest))[:limit],
        "cards_with_a_record": len(rows),
        "min_faced": min_faced,
    }


def _margin_profile(battles: list[dict]) -> dict:
    """How close the week's results actually were.

    A 0-1 loss with the opponent's last tower at 90 HP and a 0-3 sweep are both
    a loss in the tally; a three-crown win and a game survived at 31 HP are both
    a win. Crowns cannot separate them — surviving tower HP can.
    """
    wins = [b for b in battles if b.get("outcome") == "W"]
    losses = [b for b in battles if b.get("outcome") == "L"]

    def _weakest(rows, column):
        lows = [min(t) for r in rows if (t := _towers(r.get(column)))]
        return (
            sum(1 for x in lows if x < NEAR_MISS_TOWER_HP),
            min(lows) if lows else None,
            len(lows),
        )

    near_miss, closest_loss, loss_data = _weakest(losses, "opponent_princess_towers_hp_json")
    narrow, closest_win, win_data = _weakest(wins, "princess_towers_hp_json")
    return {
        "three_crown_wins": sum(1 for b in wins if int(b.get("crowns_for") or 0) >= 3),
        "no_tower_lost_wins": sum(
            1 for b in wins if len(_towers(b.get("princess_towers_hp_json"))) == 2
        ),
        "narrow_wins": narrow,
        "closest_win_own_tower_hp": closest_win,
        "one_crown_losses": sum(
            1
            for b in losses
            if int(b.get("crowns_against") or 0) - int(b.get("crowns_for") or 0) == 1
        ),
        "near_miss_losses": near_miss,
        "closest_loss_their_tower_hp": closest_loss,
        "threshold_hp": NEAR_MISS_TOWER_HP,
        "wins_with_tower_data": win_data,
        "losses_with_tower_data": loss_data,
    }


def _elixir_profile(battles: list[dict]) -> dict:
    """Elixir wasted in wins vs in losses. LOWER IS BETTER, always.

    Leaked elixir overflowed while the bar sat capped, so it is a habit rather
    than luck — and splitting by result is what makes it coaching instead of
    trivia. A member who leaks 4 in wins and 7 in losses has found something
    specific to work on. It is a CORRELATION: losing also causes passive play,
    so the report names it as a pattern, never as the cause.
    """

    def _avg(rows, column):
        vals = [
            float(r[column])
            for r in rows
            if isinstance(r.get(column), (int, float)) and not isinstance(r.get(column), bool)
        ]
        return (round(sum(vals) / len(vals), 2) if vals else None), len(vals)

    wins = [b for b in battles if b.get("outcome") == "W"]
    losses = [b for b in battles if b.get("outcome") == "L"]
    win_avg, win_n = _avg(wins, "elixir_leaked")
    loss_avg, loss_n = _avg(losses, "elixir_leaked")
    overall, overall_n = _avg(battles, "elixir_leaked")
    opp_overall, _ = _avg(battles, "opponent_elixir_leaked")
    gap = round(loss_avg - win_avg, 2) if win_avg is not None and loss_avg is not None else None
    return {
        "avg_leaked": overall,
        "avg_opponent_leaked": opp_overall,
        "in_wins": win_avg,
        "in_losses": loss_avg,
        "loss_minus_win_gap": gap,
        "battles_measured": overall_n,
        "wins_measured": win_n,
        "losses_measured": loss_n,
        "lower_is_better": True,
    }


def _tower_troop(battles: list[dict]) -> str | None:
    """The tower troop they actually played most this week."""
    counts: Counter[str] = Counter()
    for b in battles:
        for name, _mode in _deck_card_modes(b.get("support_cards_json")):
            counts[name] += 1
    return counts.most_common(1)[0][0] if counts else None


def _section_label(b: dict) -> tuple[str, str]:
    """(key, label) for the battle's report section. Special events split by their
    SPECIFIC mode — Crazy Arena and Showdown are different games and shouldn't share
    a section — while every other family (Trophy Road / River Race / Ranked / 2v2 /
    Friendly) is a single mode and stays one section."""
    mg = b.get("mode_group") or "other"
    if mg == "special_event":
        label = humanize_game_mode(b.get("game_mode_name")) or "Events"
        return label, label  # e.g. "Crazy Arena" — Title Case, never collides with a family key
    return mg, mode_group_label(mg)


def _battles_by_type(battles: list[dict]) -> dict:
    """Group the week's battles into report sections (a mode family, or a specific
    event mode), ordered by battles played. Each carries its rows (for the table)
    plus the cards the member leaned on there and their record (for the intro)."""
    groups: dict[str, dict] = {}
    for b in battles:
        key, label = _section_label(b)
        g = groups.setdefault(
            key,
            {
                "type": key,
                "label": label,
                "rows": [],
                "count": 0,
                "wins": 0,
                "losses": 0,
                "net_trophies": 0,
                "_card_counts": {},
            },
        )
        g["rows"].append(b)
        g["count"] += 1
        if b.get("outcome") == "W":
            g["wins"] += 1
        elif b.get("outcome") == "L":
            g["losses"] += 1
        g["net_trophies"] += int(b.get("trophy_change") or 0)
        for name in _deck_card_names(b.get("deck_json")):
            g["_card_counts"][name] = g["_card_counts"].get(name, 0) + 1
    for g in groups.values():
        top = sorted(g.pop("_card_counts").items(), key=lambda kv: (-kv[1], kv[0]))
        g["top_cards"] = [name for name, _ in top[:6]]
    return dict(sorted(groups.items(), key=lambda kv: -kv[1]["count"]))


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


def build_member_report_context(
    tag: str, name: str, *, days: int = 7, now: str | None = None
) -> dict:
    """Gather one member's week as pure facts — the input to both the renderer and
    the (grounded) narrative model."""
    conn = db.get_connection()
    try:
        display = preferred_display_name(conn, tag, name)
        cutoff = _cutoff(days, now)  # ISO — for *_events.observed_at
        cutoff_c = _cutoff_compact(days, now)  # compact — for battle_events.battle_time
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
        badges, cards, ranked, other, arena_changes = [], [], [], [], []
        for e in events:
            et, payload = e.get("event_type"), e.get("payload") or {}
            if et == "badge_earned":
                badges.append(
                    {
                        "label": humanize_badge(payload.get("badge_name") or ""),
                        "level": payload.get("level"),
                        "observed_at": e.get("observed_at"),
                        "image_url": payload.get("image_url"),
                    }
                )
            elif et in _CARD_EVENT_TYPES:
                cards.append(
                    {
                        "type": et,
                        "card_name": payload.get("card_name"),
                        "rarity": payload.get("rarity"),
                        "level": payload.get("level"),
                        "milestone": payload.get("milestone") or payload.get("collection_level"),
                        "observed_at": e.get("observed_at"),
                    }
                )
            elif et and et.startswith("pol_") or et in ("ultimate_champion_reached",):
                ranked.append(
                    {
                        "event_type": et,
                        "payload": payload,
                        "observed_at": e.get("observed_at"),
                    }
                )
            elif et in ("best_trophies_peak",):
                other.append(
                    {
                        "event_type": et,
                        "payload": payload,
                        "observed_at": e.get("observed_at"),
                    }
                )
            elif et == "arena_changed":
                arena_changes.append({"payload": payload, "observed_at": e.get("observed_at")})

        war = member_read.get("war")

        stream = game_events.recent_game_events(conn, days=days, now=now)
        new_cards = [s["payload"] for s in stream if s["event_type"] == "card_added"]
        new_events = [s["payload"] for s in stream if s["event_type"] == "event_started"]

        ctx = {
            "tag": tag,
            "name": display,
            "days": days,
            "window": {
                "from": cutoff,
                "generated_at": (now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            },
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
                "by_type": _battles_by_type(battles),
                # v17 depth (#216): the tally says WHAT happened, these say why.
                "scale": _battle_scale(battles),
                "matchups": _card_matchups(battles),
                "margin": _margin_profile(battles),
                "elixir": _elixir_profile(battles),
                "tower_troop": _tower_troop(battles),
            },
            "badges": badges,
            "cards": cards,
            "ranked": ranked,
            "milestones": other,
            "arena_changes": arena_changes,
            "war": war,
            "clan_standing": _battles_rank(conn, tag, cutoff_c),
            "game_stream": {
                "new_cards": new_cards,
                "new_events": new_events,
                "trending_cards": _clan_trending_cards(conn, cutoff),
            },
        }
        ctx["progress"] = _progress_items(ctx)
        return ctx
    finally:
        conn.close()


def _sig_names(sig) -> list[str]:
    if isinstance(sig, dict):
        sig = sig.get("cards") or []
    return [c.get("name") for c in (sig or []) if isinstance(c, dict) and c.get("name")][:6]


def _progress_items(ctx: dict) -> list[dict]:
    """The week's concrete progress signals as grounded ``{emoji, text}`` rows —
    new trophy peak (with week-over-week delta), arena climb, card unlocks, badges,
    card levels, collection level, Path-of-Legends. Pulled only from real events
    already read into ``ctx``; empty when the member had a quiet week."""
    prof = ctx.get("profile") or {}
    t = ctx["battles"]["tally"]
    prior = ctx["battles"].get("prior_tally") or {}
    cards = ctx.get("cards") or []
    items: list[dict] = []

    if any(m.get("event_type") == "best_trophies_peak" for m in ctx.get("milestones") or []):
        delta = f"{t['net_trophies']:+d} this week"
        if prior.get("net_trophies") is not None:
            delta += f" vs {prior['net_trophies']:+d} last"
        # Use the higher of stored best and current live trophies so the peak
        # never reads lower than the scorecard headline.
        peak = max(
            (v for v in (prof.get("best_trophies"), prof.get("trophies")) if isinstance(v, int)),
            default=None,
        )
        text = (
            f"New peak: {peak:,} ({delta})"
            if isinstance(peak, int)
            else f"New trophy peak ({delta})"
        )
        items.append({"emoji": "🏆", "text": text})

    for a in ctx.get("arena_changes") or []:
        arena = (a.get("payload") or {}).get("arena_name") or prof.get("arena")
        if arena:
            items.append({"emoji": "🏟️", "text": f"Up to {arena}"})
            break

    for c in cards:
        if c.get("type") == "card_unlocked" and c.get("card_name"):
            rarity = f" ({c['rarity']})" if c.get("rarity") else ""
            items.append({"emoji": "🆕", "text": f"Unlocked {c['card_name']}{rarity}"})

    # Badges: list notable ones individually; collapse a flurry of routine
    # card-mastery badges into a single line so a big week isn't a wall of bullets.
    badges = ctx.get("badges") or []
    mastery = [b for b in badges if str(b.get("label") or "").startswith("Card Mastery")]
    for b in badges:
        if b in mastery:
            continue
        lvl = f" (lvl {b['level']})" if b.get("level") else ""
        items.append({"emoji": "🏅", "text": f"{b['label']} badge{lvl}"})
    if len(mastery) >= 4:
        names = [str(b["label"]).split(":", 1)[-1].strip() for b in mastery[:3]]
        items.append(
            {
                "emoji": "🏅",
                "text": f"{len(mastery)} card-mastery badges ({', '.join(names)} +{len(mastery) - 3} more)",
            }
        )
    else:
        for b in mastery:
            lvl = f" (lvl {b['level']})" if b.get("level") else ""
            items.append({"emoji": "🏅", "text": f"{b['label']} badge{lvl}"})

    for c in cards:
        if c.get("type") == "card_level_milestone" and c.get("card_name"):
            lvl = c.get("level") or c.get("milestone")
            items.append(
                {
                    "emoji": "⬆️",
                    "text": f"{c['card_name']} → level {lvl}"
                    if lvl
                    else f"{c['card_name']} leveled",
                }
            )

    for c in cards:
        if c.get("type") == "collection_level_milestone" and c.get("milestone"):
            items.append({"emoji": "📚", "text": f"Collection Level {c['milestone']}"})

    for r in ctx.get("ranked") or []:
        if r.get("event_type") == "pol_promotion":
            payload = r.get("payload") or {}
            league = payload.get("league_name") or payload.get("league") or payload.get("to_league")
            items.append(
                {
                    "emoji": "⚔️",
                    "text": f"Path of Legends: {league}" if league else "Path of Legends promotion",
                }
            )

    return items


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


def _battle_types_brief(ctx: dict) -> str:
    by_type = ctx["battles"].get("by_type") or {}
    if not by_type:
        return "BATTLE TYPES THIS WEEK: none"
    out = [
        'BATTLE TYPES THIS WEEK (write one <battle_intro type="KEY"> paragraph per '
        "type below, leaning into its cards and battles played):"
    ]
    for key, g in by_type.items():
        top = ", ".join(g.get("top_cards") or []) or "n/a"
        out.append(
            f'  - type "{key}" ({g["label"]}): {g["count"]} battles, '
            f"{g['wins']}-{g['losses']}, {g['net_trophies']:+d} trophies; top cards: {top}"
        )
    return "\n".join(out)


def _progress_brief(ctx: dict) -> str | None:
    items = ctx.get("progress") or []
    if not items:
        return None
    return "PROGRESS THIS WEEK: " + "; ".join(i["text"] for i in items)


def _card_line(c: dict) -> str:
    mode = f" ({c['played_as']})" if c.get("played_as") else ""
    return f"{c['name']}{mode} {c['wins']}-{c['losses']}"


def _depth_brief(ctx: dict) -> list[str]:
    """The v17 battle facts, phrased so the model cannot invert them (#216).

    Every line states its own polarity. These are the numbers most likely to be
    narrated backwards -- praising a high elixir leak, or calling a game
    survived at 40 HP a dominant win -- and the model only sees this brief, so
    the guardrail has to live in the sentence rather than in a separate prompt.
    """
    b = ctx["battles"]
    out: list[str] = []

    m = b.get("margin") or {}
    close: list[str] = []
    if m.get("three_crown_wins"):
        close.append(f"{m['three_crown_wins']} three-crown wins")
    if m.get("no_tower_lost_wins"):
        close.append(f"{m['no_tower_lost_wins']} wins without losing a tower")
    if m.get("narrow_wins"):
        close.append(
            f"{m['narrow_wins']} wins that were nearly losses (own last tower under "
            f"{m['threshold_hp']} HP — do NOT call these dominant)"
        )
    if m.get("near_miss_losses"):
        close.append(
            f"{m['near_miss_losses']} losses where the opponent's last tower finished under "
            f"{m['threshold_hp']} HP (winnable, not outclassed)"
        )
    if m.get("closest_loss_their_tower_hp") is not None:
        close.append(f"closest loss left their tower on {m['closest_loss_their_tower_hp']} HP")
    if close:
        out.append("HOW CLOSE: " + "; ".join(close))

    e = b.get("elixir") or {}
    if e.get("avg_leaked") is not None:
        parts = [f"leaked {e['avg_leaked']} per battle vs opponents' {e['avg_opponent_leaked']}"]
        if e.get("in_wins") is not None and e.get("in_losses") is not None:
            parts.append(f"{e['in_wins']} in wins vs {e['in_losses']} in losses")
        out.append(
            "ELIXIR WASTED (LOWER IS BETTER — leaked elixir overflowed while capped; "
            "never call a high number good): " + "; ".join(parts)
        )
        gap = e.get("loss_minus_win_gap")
        if gap is not None and gap >= 1.0:
            out.append(
                f"ELIXIR PATTERN: wastes {gap} more per battle in losses than in wins — "
                "a real pattern worth naming gently, but it is a correlation, NOT a "
                "proven cause of the losses"
            )

    mu = b.get("matchups") or {}
    if mu.get("toughest"):
        out.append(
            f"TOUGHEST MATCHUPS (their record against cards faced {mu['min_faced']}+ times; "
            "these BEAT them): " + ", ".join(_card_line(c) for c in mu["toughest"])
        )
    if mu.get("best"):
        out.append(
            "BEST MATCHUPS (cards they HANDLE — a strength, never a weakness): "
            + ", ".join(_card_line(c) for c in mu["best"])
        )
    if b.get("tower_troop"):
        out.append(f"TOWER TROOP: {b['tower_troop']}")
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
        _battle_types_brief(ctx),
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
        lines.append(
            f"BATTLE OF THE WEEK: {b['outcome']} {b['crowns_for']}-{b['crowns_against']} "
            f"in {b['mode']} (trophy {int(b['trophy_change'] or 0):+d})"
        )
    lines.extend(_depth_brief(ctx))
    prog = _progress_brief(ctx)
    if prog:
        lines.append(prog)
    if ctx["war"]:
        s = (ctx["war"] or {}).get("season") or {}
        lines.append(
            f"WAR: {s.get('total_decks_used', 0)} decks used, "
            f"{s.get('total_points', 0)} points this season"
        )
    if ctx["clan_standing"]:
        cs = ctx["clan_standing"]
        lines.append(f"CLAN STANDING: #{cs['rank']} of {cs['of']} clanmates by battles played")
    tc = ctx["game_stream"].get("trending_cards") or []
    if tc:
        lines.append(
            "HOT NEW CARD IN THE CLAN THIS WEEK (fresh unlocks): "
            + ", ".join(f"{c['card_name']} ({c['members']} members)" for c in tc)
        )
    ne = ctx["game_stream"]["new_events"]
    if ne:
        lines.append("NEW EVENTS: " + ", ".join(e.get("title") or "" for e in ne))
    return "\n".join(lines)


# ── Rendering (facts → email markdown) ────────────────────────────────────────


def _fmt_dt(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%a %H:%M")
    except ValueError, TypeError:
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
    return (
        f"Here's your week: **{t['wins']}–{t['losses']}** across **{t['battles']}** battles, "
        f"a net of **{t['net_trophies']:+d}** trophies."
    )


def _fallback_standouts(ctx: dict) -> str:
    bits: list[str] = []
    b = ctx["battles"]["battle_of_week"]
    if b:
        bits.append(
            f"Your standout was a **{b['outcome']}** at {b['crowns_for']}–"
            f"{b['crowns_against']} in {b['mode']}."
        )
    ach = _achievements(ctx)
    if ach:
        bits.append("Worth noting: " + ", ".join(ach) + ".")
    tc = ctx["game_stream"].get("trending_cards") or []
    if tc:
        bits.append(
            f"And **{tc[0]['card_name']}** is the clan's card of the week "
            f"({tc[0]['members']} unlocks)."
        )
    return " ".join(bits)


def _fallback_battle_intro(g: dict) -> str:
    cards = ", ".join((g.get("top_cards") or [])[:4])
    intro = f"**{g['wins']}–{g['losses']}** across **{g['count']}** battles for **{g['net_trophies']:+d}** trophies."
    if cards:
        intro += f" Mostly on {cards}."
    return intro


def _fallback_progress(ctx: dict) -> str:
    n = len(ctx.get("progress") or [])
    return f"You stacked up {n} milestone{'s' if n != 1 else ''} worth flagging this week:"


def _matchup_row(c: dict) -> str:
    mode = f" _{c['played_as']}_" if c.get("played_as") else ""
    return (
        f"| {c['name']}{mode} | {c['wins']}–{c['losses']} | {_pct(c['win_rate'])}% | {c['faced']} |"
    )


def _render_edge(ctx: dict) -> str | None:
    """The deterministic depth block: matchup records, how close, elixir.

    Deliberately NOT left to the narrative model. These are the numbers a
    reader will act on, and a paraphrase can invert them -- so the figures are
    rendered from the facts and the model only gets to talk around them.
    """
    b = ctx["battles"]
    m = b.get("margin") or {}
    e = b.get("elixir") or {}
    mu = b.get("matchups") or {}
    block: list[str] = []

    if mu.get("toughest"):
        block += [
            "**Your matchups**",
            "",
            f"Cards you faced at least {mu['min_faced']} times this week, and how you did.",
            "",
            "| Card | Record | Win rate | Faced |",
            "|---|---|---|---|",
        ]
        block += [_matchup_row(c) for c in mu["toughest"]]
        best = [c for c in mu.get("best") or [] if c["win_rate"] > 0.5]
        seen = {(c["name"], c["played_as"]) for c in mu["toughest"]}
        best = [c for c in best if (c["name"], c["played_as"]) not in seen]
        if best:
            block.append("")
            block.append(
                "You handled these: "
                + ", ".join(f"**{c['name']}** ({c['wins']}–{c['losses']})" for c in best[:3])
                + "."
            )

    close: list[str] = []
    if m.get("near_miss_losses"):
        close.append(
            f"**{m['near_miss_losses']}** of your losses ended with their last tower under "
            f"{m['threshold_hp']} HP"
            + (
                f" — the closest left it on **{m['closest_loss_their_tower_hp']}**"
                if m.get("closest_loss_their_tower_hp") is not None
                else ""
            )
        )
    if m.get("three_crown_wins"):
        close.append(f"**{m['three_crown_wins']}** three-crown wins")
    if m.get("no_tower_lost_wins"):
        close.append(f"**{m['no_tower_lost_wins']}** wins without losing a tower")
    if m.get("narrow_wins"):
        close.append(f"**{m['narrow_wins']}** wins that were nearly losses")
    if close:
        if block:
            block.append("")
        block.append("**How close it was**")
        block.append("")
        block.extend(f"- {c}" for c in close)

    if e.get("in_wins") is not None and e.get("in_losses") is not None:
        if block:
            block.append("")
        gap = e.get("loss_minus_win_gap")
        line = (
            f"**Elixir wasted** — {e['in_wins']} per battle in your wins, "
            f"{e['in_losses']} in your losses"
        )
        if gap is not None and gap >= 1.0:
            line += f". That {gap} gap is the clearest pattern in your week"
        line += ". Lower is better — it's elixir that overflowed while you were capped."
        block.append(line)

    if b.get("tower_troop"):
        if block:
            block.append("")
        block.append(f"_Tower troop: {b['tower_troop']}_")

    return "\n".join(block) if block else None


def render_member_report(ctx: dict, narrative: dict | None = None) -> tuple[str, str]:
    """Assemble the email. The deterministic layer owns the scorecard, the progress
    milestone bullets, and a per-mode-family battle table under each type's intro;
    the model supplies the grounded narrative (overview/standouts/progress lead-in/
    meta/per-type intros/closer). No title — the subject line is the title. Returns
    (subject, markdown)."""
    nar = narrative or {}
    name = ctx["name"]
    t = ctx["battles"]["tally"]
    prof = ctx["profile"]

    parts: list[str] = []

    # Scorecard — the one stat block up top. No H1; the subject is the title.
    troph = prof.get("trophies")
    troph_str = f"{troph:,}" if isinstance(troph, int) else "—"
    parts.append(
        f"### 🏆 {troph_str}  ·  {t['wins']}–{t['losses']}  ·  "
        f"{_pct(t['win_rate'])}% win  ·  {t['battles']} battles"
    )
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

    # Progress this week — a warm lead-in plus the grounded milestone inventory.
    progress = ctx.get("progress") or []
    if progress:
        block = [
            "**Your progress this week**",
            "",
            nar.get("progress") or _fallback_progress(ctx),
            "",
        ]
        block.extend(f"- {p['emoji']} {p['text']}" for p in progress)
        parts.append("\n".join(block))

    edge = _render_edge(ctx)
    if edge:
        parts.append(edge)

    if nar.get("meta"):
        parts.append(nar["meta"])

    # Battle log, segmented by mode family — a card-aware intro then that type's table.
    intros = nar.get("battle_intros") or {}
    by_type = ctx["battles"].get("by_type") or {}
    scale = ctx["battles"].get("scale") or {
        "own_full_hp": 0,
        "opponent_full_hp": 0,
        "elixir_ceiling": 12.0,
    }
    for key, g in by_type.items():
        section = [
            f"## {g['label']} ({g['count']} battles)",
            "",
            intros.get(key) or _fallback_battle_intro(g),
            "",
            "| When | Mode | Result | Crowns | 🏆 | Towers | Elixir |",
            "|---|---|---|---|---|---|---|",
        ]
        for b in g["rows"]:
            mode = humanize_game_mode(b.get("game_mode_name")) or _mode_label(b.get("mode_group"))
            crowns = f"{b.get('crowns_for', 0)}–{b.get('crowns_against', 0)}"
            tc = b.get("trophy_change")
            tro = f"{int(tc):+d}" if tc is not None else ""
            towers = (
                f"{_tower_spark(b.get('princess_towers_hp_json'), scale['own_full_hp'])}"
                f" {_tower_spark(b.get('opponent_princess_towers_hp_json'), scale['opponent_full_hp'])}"
            )
            leaked = b.get("elixir_leaked")
            opp_leaked = b.get("opponent_elixir_leaked")
            elixir = (
                f"{_spark_char(leaked, scale['elixir_ceiling'])}"
                f"{_spark_char(opp_leaked, scale['elixir_ceiling'])}"
                if isinstance(leaked, (int, float)) and isinstance(opp_leaked, (int, float))
                else ""
            )
            section.append(
                f"| {_fmt_dt(b.get('battle_time'))} | {mode} | "
                f"{_outcome_cell(b.get('outcome'))} | {crowns} | {tro} | {towers} | {elixir} |"
            )
        # A legend per table, because these columns are unreadable without one.
        section.append("")
        section.append(
            "_**Towers** — your two princess towers, then theirs. Taller = healthier, "
            f"`{_DEAD}` = destroyed. **Elixir** — wasted by you, then them; "
            "taller = more wasted, so shorter is better._"
        )
        parts.append("\n".join(section))

    parts.append(nar.get("closer") or "Same time next week. Keep the crowns coming. — E")

    subject = f"{name} — your week in the arena 👑"
    return subject, "\n\n".join(parts)
