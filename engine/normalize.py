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


def canonical_utc_timestamp(value) -> str | None:
    """Normalize any accepted CR/ISO representation to sortable ISO-Z text.

    THE timestamp converter for data crossing the boundary from the Clash
    Royale API, whose `battleTime` is CR-compact ('20260418T153949.000Z').
    Storing that raw put a second format in a column compared as TEXT, and the
    two sort against each other in a way that is silently wrong rather than
    loudly wrong: char 4 is '0' (48) against '-' (45), so a compact value
    compares GREATER than any ISO value from the same year. A "last 7 days"
    bound written in ISO therefore matched the whole table and the resulting
    numbers looked plausible.

    The trailing 'Z' is load-bearing, not decoration. A naive
    '2026-07-30T17:30:30' is UTC only by convention, and every reader has to
    know that — which is how `engine/profiles.py` came to compare a LOCAL
    `date.today()` against UTC battle times and silently drop every battle
    after ~19:00 Chicago. Stating the zone in the data makes that mismatch
    visible. It is also what `observed_at` in the same table already carries.
    """
    dt = parse_cr_time(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


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
    day_in_week: int  # 0-6 within the section
    war_day_index: int | None  # 0-3 on battle days, None during training
    phase: str  # 'training' | 'battle'
    human: str  # "training day 2 of 3" / "battle day 3 of 4"


def war_day(period_index) -> WarDay | None:
    """CR war days are 0-based (`periodIndex % 7`: 0-2 training, 3-6 battle);
    humans say "battle day 3 of 4". Live incident 2026-07-04: the raw index
    leaked into copy as "day 2" on the third battle day.
    """
    if not isinstance(period_index, int) or period_index < 0:
        return None
    day = period_index % PERIODS_PER_SECTION
    if day < TRAINING_DAYS:
        return WarDay(
            period_index,
            day,
            None,
            "training",
            f"training day {day + 1} of {TRAINING_DAYS}",
        )
    wdi = day - TRAINING_DAYS
    return WarDay(period_index, day, wdi, "battle", f"battle day {wdi + 1} of {WAR_DAYS}")


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
    1: "Master 1",
    2: "Master 2",
    3: "Master 3",
    4: "Champion",
    5: "Grand Champion",
    6: "Royal Champion",
    7: "Ultimate Champion",
}
LEGACY_POL_LEAGUES = {  # pre-rework Path of Legends scale (docs/cr-api-docs)
    1: "Challenger I",
    2: "Challenger II",
    3: "Challenger III",
    4: "Master I",
    5: "Master II",
    6: "Master III",
    7: "Champion",
    8: "Grand Champion",
    9: "Royal Champion",
    10: "Ultimate Champion",
}
# Where the Champion tiers begin on the current scale. Leagues 1-3 are Master
# 1/2/3 — the grind — and 4 is where the game itself changes the name to
# "Champion". That naming boundary is also where the clan's own interest
# changes: over 20 days to 2026-08-04, promotions INTO leagues 1-3 reached a
# post 20% of the time and promotions into 4-6 reached one 60% of the time.
RANKED_CHAMPION_LEAGUE = 4
RANKED_UC_LEAGUE = 7  # current scheme
LEGACY_POL_UC_LEAGUE = 10  # old scheme (emitters' constant predates this)


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


# Supercell's INTERNAL card keys, which the badge API uses and the card API does
# not. A camelCase split turns these into confident-looking card names that do not
# exist -- a member's weekly email announced "Card Mastery: Witch Mother" and
# "Moving Cannon", read it as badges he had never earned, and was right to.
# 18 of 56 mastery keys we have observed split into a non-card. Values below come
# from Supercell's own `sc_key` -> name mapping, not from guesswork; internal keys
# are stable across balance patches even though stats are not.
#
# Derived exhaustively from Supercell's sc_key -> name table rather than from the
# keys we happened to observe: every card whose internal key does not split into
# its display name is here, so a badge for a card nobody has mastered yet still
# resolves the first time it appears. Event-only cards (Super*, GoblinParty*) are
# omitted -- they are not in the catalog, and the fail-closed check covers them.
#
# AxeMan is Executioner, NOT Lumberjack. A community alias map states the latter;
# Supercell's own data gives Executioner sc_key "AxeMan" and Lumberjack
# "RageBarbarian". Note also that ZapMachine is Sparky while MiniSparkys is
# Zappies -- near-swapped, and each would have named the wrong card.
_MASTERY_CARD_KEYS = {
    "AngryBarbarians": "Elite Barbarians",
    "Archer": "Archers",
    "Assassin": "Bandit",
    "AxeMan": "Executioner",
    "DartBarrell": "Flying Machine",
    "EliteArcher": "Magic Archer",
    "Ghost": "Royal Ghost",
    "Heal": "Heal Spirit",
    "IceGolemite": "Ice Golem",
    "MiniPekka": "Mini P.E.K.K.A",
    "MiniSparkys": "Zappies",
    "RageBarbarian": "Lumberjack",
    "SkeletonWarriors": "Guards",
    "Snowball": "Giant Snowball",
    "ZapMachine": "Sparky",
    "BarbLog": "Barbarian Barrel",
    "BlowdartGoblin": "Dart Goblin",
    "DarkWitch": "Night Witch",
    "FireSpirits": "Fire Spirit",
    "FirespiritHut": "Furnace",
    # Released after Supercell's published key map, so these two came from the
    # game's own gamedata.json ("internally named GiantBuffer but displays as Rune
    # Giant"; "MergeMaiden in codebase = Spirit Empress in game") and were then
    # corroborated against our own play data: both members holding MasteryGiantBuffer
    # play Rune Giant, and the only member holding MasteryMergeMaiden plays Spirit
    # Empress. The badge-holders are exactly the card-players.
    "GiantBuffer": "Rune Giant",
    "MergeMaiden": "Spirit Empress",
    "IceSpirits": "Ice Spirit",
    "Log": "The Log",
    "MovingCannon": "Cannon Cart",
    "Pekka": "P.E.K.K.A",
    "SkeletonBalloon": "Skeleton Barrel",
    "Wallbreakers": "Wall Breakers",
    "WitchMother": "Mother Witch",
    "Xbow": "X-Bow",
}


def mastery_card(badge_name, known_cards=None) -> str | None:
    """The card name behind a `Mastery<Card>` badge key, else None.

    Resolution order: Supercell's internal key map, then a camelCase split.

    ``known_cards`` -- pass the catalog's card names to make this FAIL CLOSED. A
    key we cannot resolve to a real card (a card released after the key map was
    built) returns None rather than a plausible invention, so the caller can say
    "a new Card Mastery badge" instead of naming a card that does not exist.
    Without it the split is returned unchecked, which is how the bad names shipped.
    """
    if not (
        isinstance(badge_name, str) and badge_name.startswith("Mastery") and len(badge_name) > 7
    ):
        return None
    key = badge_name[7:]
    card = _MASTERY_CARD_KEYS.get(key) or _split_camel(key) or None
    if card and known_cards is not None and card not in known_cards:
        return None
    return card


def _humanize_badge_token(tok: str) -> str:
    """One underscore-delimited badge segment → clean text. Version/season/date
    tokens stay compact (`v2`, `S2`, `2024`, `202509`→`2025-09`) rather than
    being split into `v 2` / `2025 09`; everything else is camelCase-split."""
    if re.fullmatch(r"[vV]\d+", tok):
        return tok.lower()
    if re.fullmatch(r"[A-Z]\d+", tok):
        return tok
    if re.fullmatch(r"\d{4}", tok):
        return tok
    if re.fullmatch(r"\d{6}", tok):
        return f"{tok[:4]}-{tok[4:]}"
    return _split_camel(tok)


def humanize_badge(badge_name, known_cards=None) -> str:
    """Raw API badge key → member-facing label (#167). `MasteryRonin` →
    `Card Mastery: Ronin`; known one-offs via the label map; event badges carry
    underscores and version/season suffixes (`Chaos_S2` → `Chaos S2`,
    `RoyalTournamentRank_v2` → `Royal Tournament Rank v2`), so split on `_` and
    humanize each token — a raw key never surfaces.

    Pass ``known_cards`` (the catalog's names) so an unresolvable Mastery key
    degrades to "a new Card Mastery badge" rather than naming a nonexistent card."""
    if not isinstance(badge_name, str) or not badge_name:
        return "a new badge"
    if badge_name.startswith("Mastery"):
        card = mastery_card(badge_name, known_cards)
        # Unresolvable mastery key: name the achievement, never invent the card.
        return f"Card Mastery: {card}" if card else "a new Card Mastery badge"
    if badge_name in _BADGE_LABELS:
        return _BADGE_LABELS[badge_name]
    label = " ".join(_humanize_badge_token(t) for t in badge_name.split("_") if t).strip()
    return label or badge_name


def badge_tier(level) -> str:
    """``"legendary"`` for a one-off badge, ``"routine"`` for a leveled one.

    A badge with no level is awarded once and never again — the game's notable
    tier (the Legendary badges, and the one-off event badges like `Chaos_S2`).
    A badge WITH a level is mastery/progression: it ticks up as a member grinds,
    and it is the bulk of the volume.

    Single source of truth on purpose. This predicate used to live inline in
    runtime/awareness/read.py, which meant the emitter, the gate, and the read
    each had their own opinion about what "notable" meant; the emitter now
    stamps the answer and splits the event type on it.
    """
    return "legendary" if level is None else "routine"


def ranked_league_tier(league) -> str:
    """``"master"`` | ``"champion"`` | ``"ultimate"`` for a ranked league.

    The single predicate behind the ranked-promotion event split, and the
    counterpart to :func:`badge_tier`. Both exist so that "is this notable?" is
    answered once, at the emitter, instead of re-derived by every reader with
    its own threshold.
    """
    try:
        value = int(league)
    except TypeError, ValueError:
        return "master"
    if value >= RANKED_UC_LEAGUE:
        return "ultimate"
    if value >= RANKED_CHAMPION_LEAGUE:
        return "champion"
    return "master"


def badge_facts(badge_name, catalog=None) -> dict:
    """One raw badge key → every resolved fact about it, in one place.

    Returns ``badge_label`` always, plus ``card_name``/``card_id`` when the badge
    is a Card Mastery badge for a card we can identify.

    This is the upstream form of `humanize_badge`. The badge key is the only place
    Supercell's internal card names reach us, and every surface that re-derived the
    card from the raw key got a vote on whether to do it right: the weekly email
    did, and the awareness brain did not — which is how "Dark Witch" and "Archer"
    went out to the clan on 2026-07-03 as cards a member had mastered. Resolve once
    at the emitter, stamp the answer into the event payload, and no later reader
    has to know that `MasteryDarkWitch` means Night Witch.

    ``catalog`` is ``card_catalog.card_index()`` — name → card_id. It both fails the
    resolution closed (an unknown card yields no name at all) and supplies the id,
    so the foreign key is the catalog's, never one written by hand here.
    """
    facts = {"badge_label": humanize_badge(badge_name, catalog)}
    card = mastery_card(badge_name, catalog)
    if card:
        facts["card_name"] = card
        card_id = catalog.get(card) if isinstance(catalog, dict) else None
        if card_id is not None:
            facts["card_id"] = card_id
    return facts


# Game-mode keys the CR API returns as raw snake/camelCase identifiers
# (`Crazy_Arena`, `CW_Battle_1v1`, `7xElixir_Ladder`). Same class of leak as
# badges (#167): the API never sends a member-facing label, so a raw key like
# `Crazy_Arena` must never surface. Curated labels cover the abbreviations and
# compounds a generic split mangles (CW→Clan War, TeamVsTeam→2v2); everything
# else falls through to a structural cleaner.
_GAME_MODE_LABELS = {
    "Chaos_1v1_Draft": "C.H.A.O.S Draft",
    "Crazy_Arena": "Crazy Arena",
    "Crazy_Arena_EpicOnly": "C.H.A.O.S Epic Only",
    "Crazy_Arena_InfiniteElixir": "C.H.A.O.S Infinite Elixir",
    "Chaos_1v1_TripleDraft": "C.H.A.O.S Triple Draft",
    "Crazy_Arena_SuddenDeath": "C.H.A.O.S Sudden Death",
    "Chaos_1v1_MegaDraft_All": "Ken's C.H.A.O.S Mega Draft Tournament",
    "Challenge_AllCards_EventDeck_NoSet": "All-Cards Challenge",
    "TeamVsTeam": "2v2",
    "CW_Battle_1v1": "Clan War",
    "CW_Duel_1v1": "Clan War Duel",
    "ClanWar_BoatBattle": "Boat Battle",
    "Ranked1v1_NewArena": "Ranked",
    "Ranked1v1_NewArena2": "Ranked",
    "TeamVsTeam_Touchdown_Draft": "2v2 Touchdown",
}

# A newly observed raw mode key must be curated above when the game provides an
# authoritative title, or explicitly listed here when the structural fallback
# is the approved member-facing wording. Keeping this empty is intentional:
# every new key should make the daily audit ask the question instead of silently
# normalizing a possibly wrong product name.
_APPROVED_GENERIC_GAME_MODE_KEYS: frozenset[str] = frozenset()

# Structural tokens that describe the ruleset plumbing, not the mode a member
# would name — dropped from the generic fallback so `Showdown_Friendly` reads
# "Showdown" and `Event_RestlessDead` reads "Restless Dead".
_MODE_NOISE = {
    "Ladder",
    "Friendly",
    "NoSet",
    "EventDeck",
    "AllCards",
    "Mode",
    "NewArena",
    "NewArena2",
    "Event",
    "Competitive",
}


def humanize_game_mode(mode_name) -> str | None:
    """Raw API game-mode key → member-facing label, else None for empty input.
    Curated map first; otherwise drop structural noise tokens and camelCase-split
    what remains so a raw key like `Crazy_Arena` never reaches a post."""
    if not isinstance(mode_name, str) or not mode_name:
        return None
    if mode_name in _GAME_MODE_LABELS:
        return _GAME_MODE_LABELS[mode_name]
    parts = [p for p in mode_name.split("_") if p and p not in _MODE_NOISE]
    if not parts:  # all-noise key — keep something legible
        parts = mode_name.split("_")
    label = " ".join(_split_camel(p) for p in parts).strip()
    return label or None


def game_mode_label_status(mode_name) -> tuple[str, str | None]:
    """Return the review status and member-facing label for a raw mode key.

    ``curated`` means a captured or otherwise authoritative title is in the
    explicit map. ``approved_generic`` is reserved for a reviewed structural
    fallback. Every other non-empty key is ``unreviewed`` so the objective's
    sentinel audit can surface it before it reaches member-facing copy.
    """
    if not isinstance(mode_name, str) or not mode_name:
        return "missing", None
    label = humanize_game_mode(mode_name)
    if mode_name in _GAME_MODE_LABELS:
        return "curated", label
    if mode_name in _APPROVED_GENERIC_GAME_MODE_KEYS:
        return "approved_generic", label
    return "unreviewed", label


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
