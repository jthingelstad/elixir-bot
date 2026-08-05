"""Player-stream emitters — events.md §3 (aspects profile / cards / ranked).

Milestone ladders and guards are verbatim ports of the Gen C detectors
(event_core/mind/detectors.py, line-cited in recognition.md §2): the tuned
behavior carries; the eventsourcing framework does not.

`project_player_aspects` splits one player-profile API payload into the three
aspect baselines so each aspect diffs (and dedups) independently.
"""

from __future__ import annotations

from engine import normalize
from engine.emitters import insert_stream_event
from storage import card_catalog

CARD_UNLOCK_RARITIES = {"legendary", "champion"}  # detectors.py:239 UNLOCK_RARITIES
CARD_LEVEL_MIN = 16  # detectors.py:187 MIN_LEVEL
COLLECTION_LEVEL_STEP = 100  # detectors.py:297 STEP
CAREER_WINS_STEP = 1000  # detectors.py:67 STEP
# Trophy peaks fire every 250 (was 100). A 100-trophy step made a "new personal
# best" fire on tiny increments — the 24h review saw the same climber posted for
# 13,000 then 13,142 hours later. 250 is the deterministic minimum delta before a
# peak re-surfaces; the brain applies a further per-member cooldown on top.
BEST_TROPHIES_STEP = 250  # detectors.py:53 (raised from 100, 2026-07-12)
# detectors.py:91 said 10 — the OLD Path of Legends scale. The mid-2025 rework
# has seven leagues (7 = Ultimate Champion), so the carried constant meant
# ultimate_champion_reached could never fire (found building ranked seasons,
# 2026-07-04). engine/normalize.py owns the era maps.
from engine.normalize import RANKED_UC_LEAGUE as ULTIMATE_CHAMPION_LEAGUE  # noqa: E402


def _milestones(old, new, step) -> list[int]:
    """detectors.py:14–25 verbatim: crossed milestones, guarding the old<=0
    first-observation flood."""
    if old is None or old <= 0 or new is None or new <= old:
        return []
    first = (old // step + 1) * step
    return list(range(first, new + 1, step))


def _card_level(card: dict) -> int | None:
    """Display-level normalization — delegates to the normalizer
    (engine/normalize.py), the single home for the rarity-relative math."""
    from engine.normalize import card_display_level

    return card_display_level(card.get("level"), card.get("maxLevel"))


def project_player_aspects(payload: dict) -> dict[str, dict]:
    """Split a player API payload into the three aspect baselines."""
    badges = {
        b.get("name"): {"level": b.get("level"), "progress": b.get("progress")}
        for b in payload.get("badges") or []
        if isinstance(b, dict) and b.get("name")
    }
    arena = payload.get("arena") or {}
    profile = {
        "name": payload.get("name"),
        "exp_level": payload.get("expLevel"),
        "wins": payload.get("wins"),
        "best_trophies": payload.get("bestTrophies"),
        "trophies": payload.get("trophies"),
        "arena_id": arena.get("id"),
        "arena_name": arena.get("name"),
        "badges": badges,
    }
    cards = {
        "cards": {
            str(c.get("id")): {
                "name": c.get("name"),
                "rarity": (c.get("rarity") or "").strip().lower() or None,
                "level": _card_level(c),
            }
            for c in payload.get("cards") or []
            if isinstance(c, dict) and c.get("id") is not None
        },
        "collection_level": badges.get("CollectionLevel") or {},
    }

    def _season_result(key: str) -> dict:
        r = payload.get(key) or {}
        return {
            "league": r.get("leagueNumber"),
            "rank": r.get("rank"),
            "trophies": r.get("trophies"),
        }

    pol = payload.get("currentPathOfLegendSeasonResult") or {}
    ranked = {
        "league": pol.get("leagueNumber"),
        "rank": pol.get("rank"),
        "trophies": pol.get("trophies"),
        # D6 (ranked-and-profiles.md): last/best carried so the emitter can
        # observe the monthly rollover (current resets, last swaps).
        "last": _season_result("lastPathOfLegendSeasonResult"),
        "best": _season_result("bestPathOfLegendSeasonResult"),
    }
    return {"profile": profile, "cards": cards, "ranked": ranked}


def _emit(conn, tag, observed_at, window_start, event_type, dedup_suffix, payload) -> int:
    return insert_stream_event(
        conn,
        "player_events",
        dedup_key=f"{event_type}:{tag}:{dedup_suffix}" if dedup_suffix else f"{event_type}:{tag}",
        event_type=event_type,
        subject_cols={"player_tag": tag},
        observed_at=observed_at,
        window_start=window_start,
        payload=payload,
        evidence={"aspect_window": [window_start, observed_at]},
    )


def emit_profile(conn, tag, old, new, observed_at, window_start) -> int:
    n = 0
    # NOTE: the old level_up ladder (exp_level, step 5) is retired — expLevel is
    # dead at the CR API (2026 Collection Level update). Account progression is
    # now celebrated via collection_level_milestone (emit_cards, step 100). See
    # #164 + memory cr-progression-model-2026.
    # career_wins_milestone — every 1000 (CareerWinsMilestoneDetector)
    for m in _milestones(old.get("wins"), new.get("wins"), CAREER_WINS_STEP):
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "career_wins_milestone",
            m,
            {"milestone": m, "wins": new.get("wins")},
        )
    # best_trophies_peak — every 100 (BestTrophiesPeakDetector)
    for b in _milestones(old.get("best_trophies"), new.get("best_trophies"), BEST_TROPHIES_STEP):
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "best_trophies_peak",
            b,
            {"boundary": b, "best_trophies": new.get("best_trophies")},
        )
    # badge_earned / legendary_badge_earned — newly-present badge
    # (BadgeEarnedDetector; keyed by name)
    #
    # The payload carries the RESOLVED badge alongside the raw key: badge_label,
    # and for a Card Mastery badge the card_name and card_id it refers to. The raw
    # key stays because it is what the API said and the dedup key is built from it,
    # but no reader should ever have to decode it — `MasteryDarkWitch` is Night
    # Witch, and a reader that guesses gets a real-but-wrong card. Resolved here,
    # once, against the live catalog.
    #
    # **The tier is part of the identity, so it splits the event type.** One
    # `badge_earned` type covered two populations that mean opposite things: over
    # 20 days to 2026-08-04 the clan earned 102 badges, ~40 of them "Card Mastery:
    # <card>" grind, and 4 one-off Legendary badges. The brain posted about the
    # Legendaries and correctly ignored the grind — but a wake policy keyed on
    # event type alone cannot tell them apart, so it would either wake 40 times
    # for mastery or delay the rare ones. Splitting at the emitter, where the
    # level is already known, keeps routing a property of the data instead of a
    # predicate every reader re-invents (`level is None` was being re-derived
    # downstream in runtime/awareness/read.py).
    old_badges = old.get("badges") or {}
    new_badges = {k: v for k, v in (new.get("badges") or {}).items() if k not in old_badges}
    catalog = card_catalog.card_index(conn=conn) if new_badges else {}
    for name, info in new_badges.items():
        level = (info or {}).get("level")
        tier = normalize.badge_tier(level)
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "legendary_badge_earned" if tier == "legendary" else "badge_earned",
            name,
            {
                "badge_name": name,
                "level": level,
                "badge_tier": tier,
                **normalize.badge_facts(name, catalog),
            },
        )
    # arena_changed — §11's profile-side arena-up confirmation (new in v5.1)
    old_arena, new_arena = old.get("arena_id"), new.get("arena_id")
    if new_arena is not None and old_arena is not None and new_arena != old_arena:
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "arena_changed",
            new_arena,
            {
                "arena_id": new_arena,
                "arena_name": new.get("arena_name"),
                "prev_arena_id": old_arena,
            },
        )
    return n


def emit_cards(conn, tag, old, new, observed_at, window_start) -> int:
    n = 0
    old_cards = old.get("cards") or {}
    for card_id, card in (new.get("cards") or {}).items():
        card = card or {}
        prev = old_cards.get(card_id) or {}
        # card_unlocked — legendary/champion only (NewCardUnlockedDetector).
        # new_champion_unlocked is deliberately NOT carried (events.md §6:
        # rarity payload + ledger replace the double-post path).
        if card_id not in old_cards and card.get("rarity") in CARD_UNLOCK_RARITIES:
            n += _emit(
                conn,
                tag,
                observed_at,
                window_start,
                "card_unlocked",
                card_id,
                {
                    "card_id": int(card_id),
                    "card_name": card.get("name"),
                    "rarity": card.get("rarity"),
                },
            )
        # card_level_milestone — each level ≥16 (CardLevelMilestoneDetector)
        new_level = card.get("level")
        if isinstance(new_level, int):
            old_level = prev.get("level") if isinstance(prev.get("level"), int) else -1
            for milestone in range(max(old_level + 1, CARD_LEVEL_MIN), new_level + 1):
                n += _emit(
                    conn,
                    tag,
                    observed_at,
                    window_start,
                    "card_level_milestone",
                    f"{card_id}:{milestone}",
                    {
                        "card_id": int(card_id),
                        "card_name": card.get("name"),
                        "milestone": milestone,
                    },
                )
    # collection_level_milestone — CollectionLevel badge progress, step 100
    old_cl = (old.get("collection_level") or {}).get("progress")
    new_cl = (new.get("collection_level") or {}).get("progress")
    for m in _milestones(old_cl, new_cl, COLLECTION_LEVEL_STEP):
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "collection_level_milestone",
            m,
            {"milestone": m, "collection_level": new_cl},
        )
    return n


def emit_ranked(conn, tag, old, new, observed_at, window_start) -> int:
    n = 0
    # Cold-start self-seed (ranked-and-profiles.md §2.1): any ranked diff
    # ensures the currently-open season row exists, so the first observed
    # rollover has a season to close. INSERT OR IGNORE — free after the first.
    from engine import pol_seasons

    pol_seasons.ensure_open_season(conn, observed_at)
    ol, nl = old.get("league"), new.get("league")
    orank, nrank = old.get("rank"), new.get("rank")
    # pol_promotion — league increase (PathOfLegendDetector)
    if isinstance(ol, int) and isinstance(nl, int) and nl > ol:
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "pol_promotion",
            nl,
            {"league": nl, "prev_league": ol},
        )
        # ultimate_champion_reached — crossing into league 10
        if nl == ULTIMATE_CHAMPION_LEAGUE and ol < ULTIMATE_CHAMPION_LEAGUE:
            n += _emit(
                conn,
                tag,
                observed_at,
                window_start,
                "ultimate_champion_reached",
                None,
                {"league": nl},
            )
    # pol_global_rank_attained — rank attained or improved (lower = better);
    # key = to_rank (events.md §3: key prefix = event_type, rank attained)
    from engine.normalize import pol_rank_improved

    if pol_rank_improved(orank, nrank):
        n += _emit(
            conn,
            tag,
            observed_at,
            window_start,
            "pol_global_rank_attained",
            nrank,
            {"from_rank": orank, "to_rank": nrank, "league": nl},
        )

    # Ranked season rollover (ranked-and-profiles.md §2.1): `last` changes
    # only at the monthly reset. First-diff guard (D6): a baseline written
    # before the aspect carried last/best has no "last" key — the shape
    # change alone must observe nothing.
    old_last, new_last = old.get("last"), new.get("last")
    if (
        "last" in old
        and isinstance(old_last, dict)
        and isinstance(new_last, dict)
        and old_last != new_last
        and new_last.get("league") is not None
    ):
        n += pol_seasons.observe_rollover(conn, tag, old, new, observed_at)
    return n
