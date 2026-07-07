"""The CR normalizer — the single home for Clash Royale API representational
quirks (docs/reference/v5.1/normalize.md).

Principles: (1) this module owns the quirk catalog — every rule cites
docs/cr-api-docs/ or the live incident that taught it; (2) normalization
happens at the projection boundary — the L1 raw log stays byte-true;
(3) the direct-API tool ANNOTATES (derived fields alongside raw ones,
via `annotate`) — it never mutates or hides what the API said.

Pure functions only. No DB, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

# --- war-week structure (docs/cr-api-docs/models/clans.md; verified against
# 259 archived currentriverrace payloads 2026-07: periodIndex // 7 ==
# sectionIndex; % 7 gives 0-2 training, 3-6 battle days) -----------------------
PERIODS_PER_SECTION = 7
TRAINING_DAYS = 3
WAR_DAYS = 4

# Canonical Trophy Road arenas have stable ids; above this the "arena" is the
# seasonal-league zone whose ids/names rotate monthly ("PANCAKES!", "Summit of
# Heroes") — grounded in live battle data 2026-07-03 after 7 false arena-up
# posts on go-live night. Revisit if Supercell extends the road.
ARENA_UP_MAX_CANONICAL_ID = 54000016


def parse_cr_time(value) -> datetime | None:
    """THE timestamp parser — the union of the nine local variants it replaced
    (three of which returned naive datetimes; three had live tz bugs on
    go-live night 2026-07-03).

    Accepts CR-compact ('20260703T211500.000Z', '20260703T211500Z', any
    fractional suffix), ISO 8601 (with 'Z', offset, or suffixless), or a
    datetime (passed through). Always returns tz-aware UTC — the engine
    convention: suffixless timestamps are UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        date_part = text.split("T", 1)[0]
        if "T" in text and "-" not in date_part:
            # CR compact: slice the first 15 chars ('20260703T211500') so any
            # fractional/zone suffix ('.000Z', '.000+00:00', 'Z') is tolerated.
            try:
                dt = datetime.strptime(text[:15], "%Y%m%dT%H%M%S")
            except ValueError:
                return None
        else:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def card_display_level(level, max_level) -> int | None:
    """API card levels are rarity-relative (1..maxLevel); the in-game display
    level is `level + (16 - maxLevel)` (docs/cr-api-docs/cards.md — a max-level
    common is maxLevel 14 shown as 14+2... every rarity tops out displayed as
    16 with evolutions era). Math verbatim from the pre-normalizer
    db._card_level / engine emitter copies this replaced.
    """
    if not isinstance(level, int):
        return None
    if not isinstance(max_level, int) or max_level <= 0 or max_level > 16:
        return level
    return level + max(0, 16 - max_level)


def card_display_max_level(max_level) -> int | None:
    """Display max level for a card: every rarity tops out displayed as 16
    (companion to card_display_level; found by the grep gate in
    storage/player.py during the 2026-07-04 consolidation)."""
    if not isinstance(max_level, int) or max_level <= 0:
        return None
    if max_level > 16:
        return max_level
    return max_level + max(0, 16 - max_level)


@dataclass(frozen=True)
class WarDay:
    period_index: int
    day_in_week: int              # 0-6 within the section
    war_day_index: int | None     # 0-3 on battle days, None during training
    phase: str                    # 'training' | 'battle'
    human: str                    # "training day 2 of 3" / "battle day 3 of 4"


def war_day(period_index) -> WarDay | None:
    """CR war days are 0-based (`periodIndex % 7`: 0-2 training, 3-6 battle);
    humans say "battle day 3 of 4". Live incident 2026-07-04: the raw index
    leaked into copy as "day 2" on the third battle day.
    """
    if not isinstance(period_index, int) or period_index < 0:
        return None
    day = period_index % PERIODS_PER_SECTION
    if day < TRAINING_DAYS:
        return WarDay(period_index, day, None, "training",
                      f"training day {day + 1} of {TRAINING_DAYS}")
    wdi = day - TRAINING_DAYS
    return WarDay(period_index, day, wdi, "battle",
                  f"battle day {wdi + 1} of {WAR_DAYS}")


def arena_kind(arena_id) -> str | None:
    """'road' (canonical Trophy Road, stable ids) vs 'seasonal' (monthly-themed
    league arenas — entering one is not an arena-up)."""
    if not isinstance(arena_id, int):
        return None
    return "road" if arena_id <= ARENA_UP_MAX_CANONICAL_ID else "seasonal"


# Ranked league display names span TWO epochs (catalog row 2026-07-04): the
# mid-2025 rework replaced the 10-league Path of Legends ladder with seven
# leagues, but best*SeasonResult fields keep old-scale values forever (live:
# best=10 beside a current max of 7). Display maps by era; API field names
# still say PathOfLegend*, display copy says "Ranked" (Jamie, 2026-07-04).
RANKED_LEAGUES = {
    1: "Master 1", 2: "Master 2", 3: "Master 3", 4: "Champion",
    5: "Grand Champion", 6: "Royal Champion", 7: "Ultimate Champion",
}
LEGACY_POL_LEAGUES = {  # pre-rework Path of Legends scale (docs/cr-api-docs)
    1: "Challenger I", 2: "Challenger II", 3: "Challenger III",
    4: "Master I", 5: "Master II", 6: "Master III", 7: "Champion",
    8: "Grand Champion", 9: "Royal Champion", 10: "Ultimate Champion",
}
RANKED_UC_LEAGUE = 7            # current scheme
LEGACY_POL_UC_LEAGUE = 10       # old scheme (emitters' constant predates this)


def ranked_league_name(league, *, legacy: bool = False) -> str | None:
    """League number → display name, era-aware. `legacy=True` for values known
    to predate the rework (best*SeasonResult snapshots); those get the
    '(Path of Legends era)' suffix so old honors read honestly."""
    if not isinstance(league, int):
        return None
    if legacy:
        name = LEGACY_POL_LEAGUES.get(league)
        return f"{name} (Path of Legends era)" if name else None
    return RANKED_LEAGUES.get(league)


# Badge keys the CR API returns as raw camelCase identifiers (#167). Two live
# families: `Mastery<Card>` (a Card Mastery badge, the common one) and one-off
# challenge/league badges. The API never sends a human label, so we build one —
# nothing should ever render a raw key like `MasteryRonin` to members.
_BADGE_LABELS = {
    "Classic12Wins": "Classic Challenge 12 wins",
    "Grand12Wins": "Grand Challenge 12 wins",
    "TopLeague": "Top League",
}


def _split_camel(s: str) -> str:
    """`SuspiciousBush` → `Suspicious Bush`, `CrazyArenaBadge1` → `Crazy Arena
    Badge 1`. Splits lower/digit→upper and letter→digit boundaries."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", s)
    return s.strip()


def mastery_card(badge_name) -> str | None:
    """The card name behind a `Mastery<Card>` badge key, else None. Pure
    camelCase split — `MasteryRonin` → `Ronin`, `MasterySuspiciousBush` →
    `Suspicious Bush`. Catalog canonicalization is the caller's job (this module
    stays DB-free); the split alone is human-readable for every current value."""
    if isinstance(badge_name, str) and badge_name.startswith("Mastery") and len(badge_name) > 7:
        return _split_camel(badge_name[7:]) or None
    return None


def humanize_badge(badge_name) -> str:
    """Raw API badge key → member-facing label (#167). `MasteryRonin` →
    `Card Mastery: Ronin`; known one-offs via the label map; anything else
    camelCase-split so a raw key never surfaces."""
    if not isinstance(badge_name, str) or not badge_name:
        return "a new badge"
    card = mastery_card(badge_name)
    if card:
        return f"Card Mastery: {card}"
    if badge_name in _BADGE_LABELS:
        return _BADGE_LABELS[badge_name]
    return _split_camel(badge_name) or badge_name


def pol_rank_improved(old_rank, new_rank) -> bool:
    """Path of Legends global ranks are lower-is-better (rank 1 beats 100) —
    docs/cr-api-docs/leaderboards.md. Newly attained (old None) counts."""
    if not isinstance(new_rank, int):
        return False
    if old_rank is None:
        return True
    return isinstance(old_rank, int) and new_rank < old_rank


def canon_tag(tag) -> str:
    """'#ABC123' form — the storage/identity key. Math identical to
    db._canon_tag (kept there too for its many importers; this module stays
    dependency-free to avoid db↔engine import cycles)."""
    tag = (str(tag or "")).strip().upper()
    if not tag:
        return ""
    return tag if tag.startswith("#") else f"#{tag}"


def bare_tag(tag) -> str:
    """'ABC123' form (no '#') — the URL/API-path form. Named to end the
    two-functions-one-name collision found 2026-07-04 (db added '#',
    runtime.helpers stripped it, both called _canon_tag)."""
    return canon_tag(tag).lstrip("#")


# ---------------------------------------------------------------- annotation

def _annotate_card(card) -> None:
    if isinstance(card, dict) and "level" in card and "display_level" not in card:
        dl = card_display_level(card.get("level"), card.get("maxLevel"))
        if dl is not None:
            card["display_level"] = dl


def _annotate_cards_in(container, *keys) -> None:
    if not isinstance(container, dict):
        return
    for key in keys:
        value = container.get(key)
        if isinstance(value, list):
            for card in value:
                _annotate_card(card)


def annotate(payload, endpoint: str | None):
    """Attach derived fields ALONGSIDE raw API fields — never replace, never
    hide. The direct-CR-API tool passes its (filtered) responses through here
    so the LLM sees both the true API value and the human meaning. Unknown
    endpoint shapes pass through untouched.
    """
    if not endpoint:
        return payload
    if endpoint in ("player_battles", "player_battlelog"):
        # The raw battlelog is a top-level list; the tool's filtered form
        # wraps it as {"battles": [...]}. Handle both.
        if isinstance(payload, list):
            battles = payload
        elif isinstance(payload, dict) and isinstance(payload.get("battles"), list):
            battles = payload["battles"]
        else:
            battles = []
        for battle in battles:
            if not isinstance(battle, dict):
                continue
            for side in ("team", "opponent"):
                for participant in battle.get(side) or []:
                    _annotate_cards_in(participant, "cards", "currentDeck")
        return payload
    if not isinstance(payload, dict):
        return payload
    if endpoint in ("player", "player_profile"):
        _annotate_cards_in(payload, "cards", "currentDeck")
    elif endpoint in ("clan_war", "currentriverrace", "riverrace"):
        period_index = payload.get("periodIndex")
        wd = war_day(period_index)
        if wd is not None and "day_human" not in payload:
            payload["day_human"] = wd.human
            payload["day_phase"] = wd.phase
    return payload


def period_anchor_from_events(conn, season_id, section_index, day_index):
    """Drift-adaptive war-day anchor — lives in engine.clock (it needs the
    WarClock context); re-exported here so the quirk catalog has one front
    door. Function-level import avoids the clock→normalize→clock cycle."""
    from engine.clock import period_anchor_from_events as _anchor

    return _anchor(conn, season_id, section_index, day_index)
