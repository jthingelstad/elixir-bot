"""Observed deck and clan-local metagame intelligence.

Storage supplies battle rows and card facts.  This capability owns the stable
meaning: exact deck identity, primary/variant selection, archetype labels,
stability, observed performance, upgrade bottlenecks, and clan-local spread.
It never pretends Elixir has opponent deck lists or a global ladder dataset.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

import db as db_facade
from capabilities.contracts import DeckIntelligenceResult

CAPABILITY_ID = "deck_intelligence"
CONTRACT_VERSION = 1
log = logging.getLogger("elixir.capabilities.decks")

_WIN_CONDITIONS = {
    "Balloon",
    "Battle Ram",
    "Electro Giant",
    "Elixir Golem",
    "Giant",
    "Goblin Barrel",
    "Goblin Drill",
    "Goblin Giant",
    "Golem",
    "Graveyard",
    "Hog Rider",
    "Lava Hound",
    "Miner",
    "Mortar",
    "Ram Rider",
    "Royal Giant",
    "Royal Hogs",
    "Skeleton Barrel",
    "Three Musketeers",
    "Wall Breakers",
    "X-Bow",
}
_SIEGE = {"X-Bow", "Mortar"}
_HEAVY_BEATDOWN = {
    "Golem",
    "Lava Hound",
    "Electro Giant",
    "Goblin Giant",
    "Giant",
    "Elixir Golem",
}
_BRIDGE_PRESSURE = {"Battle Ram", "Ram Rider", "Royal Hogs"}


def _source(source):
    return source or db_facade


def _invoke(source, name: str, *args, conn=None, **kwargs):
    if conn is not None:
        kwargs["conn"] = conn
    return getattr(source, name)(*args, **kwargs)


def _tag(value: str) -> str:
    clean = str(value or "").strip().upper()
    return clean if clean.startswith("#") else f"#{clean}"


def _card_name(card: dict) -> str | None:
    value = card.get("name") if isinstance(card, dict) else None
    return str(value).strip() if value else None


def _fingerprint(cards: Iterable[dict]) -> tuple[str, ...]:
    tokens = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        card_id = card.get("id")
        name = _card_name(card)
        token = f"id:{card_id}" if card_id is not None else f"name:{(name or '').lower()}"
        if name or card_id is not None:
            tokens.append(token)
    return tuple(sorted(set(tokens)))


def _names(cards: Iterable[dict]) -> list[str]:
    named = (_card_name(card) for card in cards or [])
    return sorted({name for name in named if name})


def _elixir_cost(card: dict) -> float | None:
    """Either spelling the CR API uses, or None."""
    value = card.get("elixir_cost")
    if value is None:
        value = card.get("elixirCost")
    return float(value) if isinstance(value, (int, float)) else None


def _average_elixir(cards: Iterable[dict]) -> float | None:
    """Mean deck cost, or None unless EVERY card has a readable cost.

    All-or-nothing on purpose: averaging the subset that happens to carry a
    cost silently reports a cheaper deck than the member is playing.
    """
    playable = [card for card in cards or [] if isinstance(card, dict)]
    costs = [cost for cost in (_elixir_cost(card) for card in playable) if cost is not None]
    if not playable or len(costs) != len(playable):
        return None
    return round(sum(costs) / len(costs), 2)


def _battle_count(rows: list[dict]) -> int:
    keys = {row.get("dedup_key") for row in rows if row.get("dedup_key")}
    return len(keys) if keys else len(rows)


def _archetype(cards: Iterable[dict]) -> dict:
    names = set(_names(cards))
    win_conditions = sorted(names & _WIN_CONDITIONS)
    average = _average_elixir(cards)
    if names & _SIEGE:
        label = "siege"
    elif "Goblin Barrel" in names and names & {
        "Princess",
        "Dart Goblin",
        "Skeleton Barrel",
    }:
        label = "bait"
    elif names & _HEAVY_BEATDOWN:
        label = "beatdown"
    elif "Graveyard" in names:
        label = "graveyard control"
    elif "Royal Giant" in names:
        label = "royal giant control"
    elif "Balloon" in names:
        label = "balloon cycle" if average is not None and average <= 3.4 else "balloon beatdown"
    elif names & _BRIDGE_PRESSURE and names & {"Bandit", "Royal Ghost", "P.E.K.K.A"}:
        label = "bridge spam"
    elif names & {"Hog Rider", "Goblin Drill", "Miner", "Wall Breakers"}:
        label = "cycle" if average is not None and average <= 3.4 else "control"
    elif average is not None and average <= 3.1:
        label = "cycle"
    else:
        label = "hybrid"
    return {
        "label": label,
        "win_conditions": win_conditions,
        "average_elixir": average,
        "classification_basis": "observed cards + catalog elixir costs",
    }


def _deck_groups(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, ...], dict] = {}
    for row in rows:
        cards = row.get("cards") or []
        fingerprint = _fingerprint(cards)
        if not fingerprint:
            continue
        group = groups.setdefault(
            fingerprint,
            {
                "fingerprint": "|".join(fingerprint),
                "cards": cards,
                "battles": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "trophy_delta": 0,
                "latest_battle_at": None,
                "modes": Counter(),
                "outcome_granularity": Counter(),
            },
        )
        group["battles"] += 1
        outcome = row.get("outcome")
        if outcome == "W":
            group["wins"] += 1
        elif outcome == "L":
            group["losses"] += 1
        else:
            group["draws"] += 1
        group["trophy_delta"] += int(row.get("trophy_change") or 0)
        group["latest_battle_at"] = (
            max(group["latest_battle_at"] or "", str(row.get("battle_time") or "")) or None
        )
        group["modes"][str(row.get("mode_group") or "unknown")] += 1
        group["outcome_granularity"][str(row.get("outcome_granularity") or "battle")] += 1

    result = []
    for group in groups.values():
        battles = group["battles"]
        result.append(
            {
                "fingerprint": group["fingerprint"],
                "cards": [
                    {
                        key: card.get(key)
                        for key in ("id", "name", "level", "elixir_cost")
                        if card.get(key) is not None
                    }
                    for card in group["cards"]
                ],
                "card_names": _names(group["cards"]),
                "archetype": _archetype(group["cards"]),
                "battles": battles,
                "wins": group["wins"],
                "losses": group["losses"],
                "draws": group["draws"],
                "win_rate": round(group["wins"] / battles, 3) if battles else None,
                "trophy_delta": group["trophy_delta"],
                "latest_battle_at": group["latest_battle_at"],
                "modes": dict(group["modes"].most_common()),
                "outcome_granularity": dict(group["outcome_granularity"].most_common()),
            }
        )
    return result


def _sorted_groups(rows: list[dict]) -> list[dict]:
    groups = _deck_groups(rows)
    return sorted(
        groups,
        key=lambda item: (item["battles"], item.get("latest_battle_at") or ""),
        reverse=True,
    )


def _variants(primary: dict | None, groups: list[dict]) -> list[dict]:
    if not primary:
        return []
    base = set(primary.get("card_names") or [])
    result = []
    for group in groups:
        if group["fingerprint"] == primary["fingerprint"]:
            continue
        names = set(group.get("card_names") or [])
        if len(base & names) < 6:
            continue
        result.append(
            {
                "archetype": group["archetype"],
                "battles": group["battles"],
                "wins": group["wins"],
                "losses": group["losses"],
                "win_rate": group["win_rate"],
                "added": sorted(names - base),
                "removed": sorted(base - names),
                "latest_battle_at": group["latest_battle_at"],
            }
        )
    return result[:5]


def _recent_change(rows: list[dict], groups: list[dict]) -> dict | None:
    if not rows:
        return None
    latest_fp = _fingerprint(rows[0].get("cards") or [])
    latest_battle_key = rows[0].get("dedup_key")
    previous_fp = None
    for row in rows[1:]:
        if latest_battle_key and row.get("dedup_key") == latest_battle_key:
            continue
        candidate = _fingerprint(row.get("cards") or [])
        if candidate and candidate != latest_fp:
            previous_fp = candidate
            break
    if previous_fp is None:
        return None
    by_fp = {tuple(group["fingerprint"].split("|")): group for group in groups}
    latest = by_fp.get(latest_fp)
    previous = by_fp.get(previous_fp)
    if not latest or not previous:
        return None
    latest_names = set(latest["card_names"])
    previous_names = set(previous["card_names"])
    return {
        "added": sorted(latest_names - previous_names),
        "removed": sorted(previous_names - latest_names),
        "latest_deck": {
            key: latest[key]
            for key in ("battles", "wins", "losses", "win_rate", "latest_battle_at")
        },
        "previous_deck": {
            key: previous[key]
            for key in ("battles", "wins", "losses", "win_rate", "latest_battle_at")
        },
        "interpretation": "observed association, not proof that the card swap caused the result",
    }


def _upgrade_bottlenecks(source, player_tag: str, *, conn=None) -> list[dict]:
    try:
        result = _invoke(
            source,
            "lookup_member_cards",
            player_tag,
            filter={"deck": True},
            limit=8,
            conn=conn,
        )
    except Exception:
        # An empty deck list is indistinguishable from "this member has no
        # decks" once it reaches the model, so a break here becomes Elixir
        # confidently telling a member it found nothing.
        log.warning("deck lookup failed for %s — answering as if empty", player_tag, exc_info=True)
        return []
    cards = result.get("cards") if isinstance(result, dict) else []
    ranked = sorted(
        cards or [],
        key=lambda card: (
            -(card.get("king_tower_gap") or 0),
            -(card.get("levels_to_max") or 0),
            card.get("name") or "",
        ),
    )
    return [
        {
            key: card.get(key)
            for key in (
                "name",
                "level",
                "king_tower_gap",
                "levels_to_max",
                "ready_to_upgrade",
                "cards_required_for_next_level",
            )
            if card.get(key) is not None
        }
        for card in ranked[:5]
        if (card.get("king_tower_gap") or 0) > 0 or (card.get("levels_to_max") or 0) > 0
    ]


def _member_view(
    player_tag: str, rows: list[dict], source, *, days: int, scope: str, conn=None
) -> DeckIntelligenceResult:
    groups = _sorted_groups(rows)
    primary = groups[0] if groups else None
    total = len(rows)
    share = round(primary["battles"] / total, 3) if primary and total else None
    if share is None:
        stability = "unobserved"
    elif share >= 0.6:
        stability = "stable"
    elif share >= 0.35:
        stability = "mixed"
    else:
        stability = "experimenting"
    if scope == "war":
        current = None
    else:
        try:
            current = _invoke(source, "get_member_current_deck", player_tag, conn=conn)
        except Exception:
            log.debug(
                "current deck enrichment unavailable player_tag=%s",
                player_tag,
                exc_info=True,
            )
            current = None
    current_summary = None
    if isinstance(current, dict) and current.get("cards"):
        fp = "|".join(_fingerprint(current["cards"]))
        match = next((group for group in groups if group["fingerprint"] == fp), None)
        current_summary = {
            "observed_at": current.get("fetched_at"),
            "card_names": _names(current["cards"]),
            # The tower troop is part of a modern CR deck, not a cosmetic. It
            # was captured at v18 (#216) but this view still dropped it, so a
            # "what deck does X run" answer silently omitted a real choice --
            # 6.9% of the clan runs something other than Tower Princess.
            "tower_troop": _names(current.get("support_cards") or []) or None,
            "archetype": _archetype(current["cards"]),
            "historical_performance": (
                {
                    key: match[key]
                    for key in ("battles", "wins", "losses", "win_rate", "trophy_delta")
                }
                if match
                else None
            ),
        }
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": "member",
        "available": bool(rows or current_summary),
        "player_tag": player_tag,
        "player_name": rows[0].get("player_name") if rows else None,
        "window": {
            "days": days,
            "scope": scope,
            "sample_battles": _battle_count(rows),
            "deck_observations": total,
        },
        "current_deck": current_summary,
        "current_deck_note": (
            "The profile current deck is a Trophy Road deck and is omitted in war scope."
            if scope == "war"
            else None
        ),
        "primary_deck": primary,
        "variants": _variants(primary, groups),
        "recent_change": _recent_change(rows, groups),
        "stability": {
            "label": stability,
            "primary_deck_share": share,
            "distinct_decks": len(groups),
        },
        "upgrade_bottlenecks": _upgrade_bottlenecks(source, player_tag, conn=conn),
        "evidence_limits": {
            "opponent_decks_captured": False,
            "global_meta_claims_supported": False,
            "note": "Performance is association over observed own decks; opponent deck lists are unavailable.",
        },
        "sources": [
            "battle_events.deck_json",
            "card_catalog",
            "player_current_state",
            "player_card_collection",
        ],
    }


def _clan_view(rows: list[dict], *, days: int, scope: str) -> DeckIntelligenceResult:
    by_player: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_player[row["player_tag"]].append(row)
    members = []
    archetypes: dict[str, dict] = {}
    win_conditions: Counter = Counter()
    common_cards: Counter = Counter()
    for tag, player_rows in by_player.items():
        groups = _sorted_groups(player_rows)
        if not groups:
            continue
        primary = groups[0]
        label = primary["archetype"]["label"]
        bucket = archetypes.setdefault(label, {"members": 0, "battles": 0, "wins": 0})
        bucket["members"] += 1
        bucket["battles"] += primary["battles"]
        bucket["wins"] += primary["wins"]
        win_conditions.update(primary["archetype"]["win_conditions"])
        common_cards.update(set(primary["card_names"]))
        members.append(
            {
                "player_tag": tag,
                "player_name": player_rows[0].get("player_name"),
                "archetype": label,
                "win_conditions": primary["archetype"]["win_conditions"],
                "battles": primary["battles"],
                "win_rate": primary["win_rate"],
                "primary_deck_share": round(primary["battles"] / len(player_rows), 3),
            }
        )
    archetype_rows = []
    for label, item in archetypes.items():
        archetype_rows.append(
            {
                "archetype": label,
                **item,
                "win_rate": round(item["wins"] / item["battles"], 3) if item["battles"] else None,
            }
        )
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": "clan",
        "available": bool(members),
        "window": {
            "days": days,
            "scope": scope,
            "sample_battles": _battle_count(rows),
            "deck_observations": len(rows),
        },
        "coverage": {"members_with_observed_decks": len(members)},
        "archetype_spread": sorted(
            archetype_rows, key=lambda item: (-item["members"], item["archetype"])
        ),
        "win_condition_spread": [
            {"card": card, "primary_decks": count} for card, count in win_conditions.most_common(15)
        ],
        "common_primary_deck_cards": [
            {"card": card, "primary_decks": count} for card, count in common_cards.most_common(20)
        ],
        "members": sorted(members, key=lambda item: (-item["battles"], item["player_name"] or "")),
        "evidence_limits": {
            "scope": "POAP KINGS observed decks only; this is clan-local meta, not global ladder meta",
            "opponent_decks_captured": False,
        },
        "sources": ["battle_events.deck_json", "card_catalog", "clan_memberships"],
    }


def _change_date(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalize_changes(
    cards: list[str] | None,
    changes: list[dict] | None,
    *,
    as_of: datetime,
) -> list[dict]:
    normalized: dict[str, dict] = {}
    for raw in changes or []:
        if not isinstance(raw, dict):
            continue
        card = str(raw.get("card") or raw.get("card_name") or "").strip()
        if not card:
            continue
        published_at = raw.get("published_at") or raw.get("source_date")
        published = _change_date(published_at)
        status = str(raw.get("status") or "unknown").strip().lower()
        stale = bool(published and published < as_of - timedelta(days=90))
        if status in {"stale", "superseded"}:
            stale = True
        if stale and status in {"wip", "work_in_progress", "proposed"}:
            source_state = "stale_wip"
        elif stale:
            source_state = "stale"
        elif status in {"wip", "work_in_progress", "proposed"}:
            source_state = "wip"
        elif published_at and raw.get("source_url"):
            source_state = "current"
        else:
            source_state = "unknown"
        direction = str(raw.get("direction") or "unknown").strip().lower()
        if direction not in {"buff", "nerf", "rework", "mixed", "unknown"}:
            direction = "unknown"
        normalized[card.lower()] = {
            "card": card,
            "direction": direction,
            "status": status,
            "source_url": raw.get("source_url"),
            "published_at": published_at,
            "effective_at": raw.get("effective_at"),
            "source_state": source_state,
            "stale": stale,
        }
    for card in cards or []:
        name = str(card).strip()
        if not name or name.lower() in normalized:
            continue
        normalized[name.lower()] = {
            "card": name,
            "direction": "unknown",
            "status": "unknown",
            "source_url": None,
            "published_at": None,
            "effective_at": None,
            "source_state": "unknown",
            "stale": False,
        }
    return list(normalized.values())


def _card_impact(
    rows: list[dict],
    current_decks: list[dict],
    changes: list[dict],
    *,
    days: int,
    scope: str,
) -> DeckIntelligenceResult:
    targets = {change["card"].lower(): change for change in changes}
    by_player: dict[str, dict] = {}
    for row in rows:
        names = set(_names(row.get("cards") or []))
        matched = sorted(name for name in names if name.lower() in targets)
        if not matched:
            continue
        item = by_player.setdefault(
            row["player_tag"],
            {
                "player_tag": row["player_tag"],
                "player_name": row.get("player_name"),
                "matching_cards": set(),
                "battles": 0,
                "wins": 0,
                "losses": 0,
                "latest_battle_at": None,
                "current_deck_observed_at": None,
                "evidence_types": set(),
            },
        )
        item["matching_cards"].update(matched)
        item["battles"] += 1
        item["wins"] += 1 if row.get("outcome") == "W" else 0
        item["losses"] += 1 if row.get("outcome") == "L" else 0
        item["latest_battle_at"] = max(item["latest_battle_at"] or "", row.get("battle_time") or "")
        item["evidence_types"].add("recent_battle_deck")
    for deck in current_decks:
        names = set(_names(deck.get("cards") or []))
        matched = sorted(name for name in names if name.lower() in targets)
        if not matched:
            continue
        item = by_player.setdefault(
            deck["player_tag"],
            {
                "player_tag": deck["player_tag"],
                "player_name": deck.get("player_name"),
                "matching_cards": set(),
                "battles": 0,
                "wins": 0,
                "losses": 0,
                "latest_battle_at": None,
                "current_deck_observed_at": None,
                "evidence_types": set(),
            },
        )
        item["matching_cards"].update(matched)
        item["current_deck_observed_at"] = deck.get("observed_at")
        item["evidence_types"].add("current_profile_deck")
    members = []
    for item in by_player.values():
        item["matching_cards"] = sorted(item["matching_cards"])
        item["evidence_types"] = sorted(item["evidence_types"])
        item["matching_changes"] = [targets[name.lower()] for name in item["matching_cards"]]
        item["win_rate"] = round(item["wins"] / item["battles"], 3) if item["battles"] else None
        members.append(item)
    observed = {name.lower() for member in members for name in member["matching_cards"]}
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "view": "card_impact",
        "available": bool(changes),
        "changes": changes,
        "cards": [change["card"] for change in changes],
        "window": {
            "days": days,
            "scope": scope,
            "sample_battles": _battle_count(rows),
            "deck_observations": len(rows),
        },
        "affected_members": sorted(
            members, key=lambda item: (-item["battles"], item["player_name"] or "")
        ),
        "affected_member_count": len(members),
        "changes_without_member_evidence": [
            change for key, change in targets.items() if key not in observed
        ],
        "interpretation": "Who actually uses the named cards and their observed results; does not infer the balance change itself.",
        "evidence_limits": {
            "global_meta_claims_supported": False,
            "opponent_decks_captured": False,
        },
        "sources": [
            "battle_events.deck_json",
            "player_current_state.current_deck_json",
            "clan_memberships",
        ],
    }


def get_deck_intelligence(
    *,
    view: str = "member",
    player_tag: str | None = None,
    cards: list[str] | None = None,
    changes: list[dict] | None = None,
    days: int = 30,
    scope: str = "competitive",
    source=None,
    conn=None,
    as_of: datetime | None = None,
) -> DeckIntelligenceResult:
    """Return member, clan-local meta, or named-card impact intelligence."""
    source = _source(source)
    days = max(1, min(int(days or 30), 180))
    if scope not in {"all", "competitive", "ladder_ranked", "ladder", "ranked", "war"}:
        return {
            "capability": CAPABILITY_ID,
            "contract_version": CONTRACT_VERSION,
            "available": False,
            "error": "unsupported_scope",
            "scope": scope,
        }
    if view == "member":
        if not player_tag:
            return {
                "capability": CAPABILITY_ID,
                "contract_version": CONTRACT_VERSION,
                "available": False,
                "error": "player_tag_required",
            }
        tag = _tag(player_tag)
        rows = (
            _invoke(
                source,
                "list_deck_battle_history",
                tag,
                days=days,
                scope=scope,
                limit=1200,
                conn=conn,
            )
            or []
        )
        return _member_view(tag, rows, source, days=days, scope=scope, conn=conn)
    history_tag = _tag(player_tag) if view == "card_impact" and player_tag else None
    rows = (
        _invoke(
            source,
            "list_deck_battle_history",
            history_tag,
            days=days,
            scope=scope,
            limit=5000,
            conn=conn,
        )
        or []
    )
    if view == "clan":
        return _clan_view(rows, days=days, scope=scope)
    if view == "card_impact":
        normalized_changes = _normalize_changes(
            cards,
            changes,
            as_of=as_of or datetime.now(timezone.utc),
        )
        if not normalized_changes:
            return {
                "capability": CAPABILITY_ID,
                "contract_version": CONTRACT_VERSION,
                "available": False,
                "error": "changes_or_cards_required",
            }
        try:
            current_decks = (
                _invoke(source, "list_current_member_decks", history_tag, conn=conn) or []
            )
        except AttributeError, TypeError:
            current_decks = []
        result = _card_impact(
            rows,
            current_decks,
            normalized_changes,
            days=days,
            scope=scope,
        )
        if history_tag:
            result["player_tag"] = history_tag
        return result
    return {
        "capability": CAPABILITY_ID,
        "contract_version": CONTRACT_VERSION,
        "available": False,
        "error": "unsupported_view",
        "view": view,
    }


__all__ = ["CAPABILITY_ID", "CONTRACT_VERSION", "get_deck_intelligence"]
