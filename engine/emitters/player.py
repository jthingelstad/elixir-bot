"""Player-stream emitters — events.md §3 (aspects profile / cards / ranked).

Milestone ladders and guards are verbatim ports of the Gen C detectors
(event_core/mind/detectors.py, line-cited in recognition.md §2): the tuned
behavior carries; the eventsourcing framework does not.

`project_player_aspects` splits one player-profile API payload into the three
aspect baselines so each aspect diffs (and dedups) independently.
"""

from __future__ import annotations

from engine.emitters import insert_stream_event

CARD_UNLOCK_RARITIES = {"legendary", "champion"}  # detectors.py:239 UNLOCK_RARITIES
CARD_LEVEL_MIN = 16          # detectors.py:187 MIN_LEVEL
COLLECTION_LEVEL_STEP = 100  # detectors.py:297 STEP
CAREER_WINS_STEP = 1000      # detectors.py:67 STEP
LEVEL_UP_STEP = 5            # detectors.py:35 _milestones(step=5)
BEST_TROPHIES_STEP = 100     # detectors.py:53
ULTIMATE_CHAMPION_LEAGUE = 10  # detectors.py:91


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
    pol = payload.get("currentPathOfLegendSeasonResult") or {}
    ranked = {
        "league": pol.get("leagueNumber"),
        "rank": pol.get("rank"),
        "trophies": pol.get("trophies"),
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
    # level_up — every 5 levels (PlayerLevelUpDetector)
    for level in _milestones(old.get("exp_level"), new.get("exp_level"), LEVEL_UP_STEP):
        n += _emit(conn, tag, observed_at, window_start, "level_up", level,
                   {"level": level, "prev_level": old.get("exp_level")})
    # career_wins_milestone — every 1000 (CareerWinsMilestoneDetector)
    for m in _milestones(old.get("wins"), new.get("wins"), CAREER_WINS_STEP):
        n += _emit(conn, tag, observed_at, window_start, "career_wins_milestone", m,
                   {"milestone": m, "wins": new.get("wins")})
    # best_trophies_peak — every 100 (BestTrophiesPeakDetector)
    for b in _milestones(old.get("best_trophies"), new.get("best_trophies"), BEST_TROPHIES_STEP):
        n += _emit(conn, tag, observed_at, window_start, "best_trophies_peak", b,
                   {"boundary": b, "best_trophies": new.get("best_trophies")})
    # badge_earned — newly-present badge (BadgeEarnedDetector; keyed by name)
    old_badges = old.get("badges") or {}
    for name, info in (new.get("badges") or {}).items():
        if name not in old_badges:
            n += _emit(conn, tag, observed_at, window_start, "badge_earned", name,
                       {"badge_name": name, "level": (info or {}).get("level")})
    # arena_changed — §11's profile-side arena-up confirmation (new in v5.1)
    old_arena, new_arena = old.get("arena_id"), new.get("arena_id")
    if new_arena is not None and old_arena is not None and new_arena != old_arena:
        n += _emit(conn, tag, observed_at, window_start, "arena_changed", new_arena,
                   {"arena_id": new_arena, "arena_name": new.get("arena_name"),
                    "prev_arena_id": old_arena})
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
            n += _emit(conn, tag, observed_at, window_start, "card_unlocked", card_id,
                       {"card_id": int(card_id), "card_name": card.get("name"),
                        "rarity": card.get("rarity")})
        # card_level_milestone — each level ≥16 (CardLevelMilestoneDetector)
        new_level = card.get("level")
        if isinstance(new_level, int):
            old_level = prev.get("level") if isinstance(prev.get("level"), int) else -1
            for milestone in range(max(old_level + 1, CARD_LEVEL_MIN), new_level + 1):
                n += _emit(conn, tag, observed_at, window_start, "card_level_milestone",
                           f"{card_id}:{milestone}",
                           {"card_id": int(card_id), "card_name": card.get("name"),
                            "milestone": milestone})
    # collection_level_milestone — CollectionLevel badge progress, step 100
    old_cl = (old.get("collection_level") or {}).get("progress")
    new_cl = (new.get("collection_level") or {}).get("progress")
    for m in _milestones(old_cl, new_cl, COLLECTION_LEVEL_STEP):
        n += _emit(conn, tag, observed_at, window_start, "collection_level_milestone", m,
                   {"milestone": m, "collection_level": new_cl})
    return n


def emit_ranked(conn, tag, old, new, observed_at, window_start) -> int:
    n = 0
    ol, nl = old.get("league"), new.get("league")
    orank, nrank = old.get("rank"), new.get("rank")
    # pol_promotion — league increase (PathOfLegendDetector)
    if isinstance(ol, int) and isinstance(nl, int) and nl > ol:
        n += _emit(conn, tag, observed_at, window_start, "pol_promotion", nl,
                   {"league": nl, "prev_league": ol})
        # ultimate_champion_reached — crossing into league 10
        if nl == ULTIMATE_CHAMPION_LEAGUE and ol < ULTIMATE_CHAMPION_LEAGUE:
            n += _emit(conn, tag, observed_at, window_start, "ultimate_champion_reached", None,
                       {"league": nl})
    # pol_global_rank_attained — rank attained or improved (lower = better);
    # key = to_rank (events.md §3: key prefix = event_type, rank attained)
    from engine.normalize import pol_rank_improved

    if pol_rank_improved(orank, nrank):
        n += _emit(conn, tag, observed_at, window_start, "pol_global_rank_attained", nrank,
                   {"from_rank": orank, "to_rank": nrank, "league": nl})
    return n
