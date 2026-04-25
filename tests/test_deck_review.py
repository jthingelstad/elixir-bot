"""Tests for the deck_review workflow: opponent capture, losses aggregation,
war deck reconstruction, request classification, and war-suggest validation.
"""

import json

import db


def _make_card(name, level=14, max_level=14, elixir=3, rarity="common", evolution_level=None):
    card = {
        "name": name,
        "id": hash(name) & 0xFFFFFFF,
        "level": level,
        "maxLevel": max_level,
        "elixirCost": elixir,
        "rarity": rarity,
        "iconUrls": {"medium": f"https://example.test/{name}.png"},
    }
    if evolution_level is not None:
        card["evolutionLevel"] = evolution_level
    return card


def _deck(*names):
    return [_make_card(n) for n in names]


def _battle(battle_time, *, battle_type="riverRacePvP", outcome_crowns=(1, 0),
            team_cards=None, opp_cards=None, team_rounds=None, opp_rounds=None,
            deck_selection="warDeckPick"):
    crowns_for, crowns_against = outcome_crowns
    return {
        "type": battle_type,
        "battleTime": battle_time,
        "gameMode": {"id": 1, "name": "test"},
        "deckSelection": deck_selection,
        "arena": {"id": 1, "name": "Arena"},
        "team": [{
            "tag": "#PLAYER",
            "name": "Player",
            "crowns": crowns_for,
            "cards": team_cards or [],
            "supportCards": [],
            "rounds": team_rounds or [],
        }],
        "opponent": [{
            "tag": "#OPP",
            "name": "Opponent",
            "crowns": crowns_against,
            "cards": opp_cards or [],
            "supportCards": [],
            "rounds": opp_rounds or [],
        }],
    }


# ── Phase 1: opponent deck capture ────────────────────────────────────────────

def test_opponent_deck_captured_on_battlelog_ingest():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        opp_cards = _deck("Hog Rider", "Musketeer", "Cannon", "Ice Spirit",
                          "Skeletons", "Fireball", "Log", "Ice Golem")
        db.snapshot_player_battlelog(
            "#PLAYER",
            [_battle("20260401T120000.000Z", battle_type="PvP", outcome_crowns=(0, 2),
                    team_cards=_deck("Knight"), opp_cards=opp_cards, deck_selection="collection")],
            conn=conn,
        )
        row = conn.execute(
            "SELECT opponent_deck_json FROM member_battle_facts"
        ).fetchone()
        stored = json.loads(row["opponent_deck_json"])
        names = [c["name"] for c in stored]
        assert "Hog Rider" in names and len(stored) == 8
    finally:
        conn.close()


# ── Phase 2a: get_member_recent_losses ────────────────────────────────────────

def test_get_member_recent_losses_aggregates_top_opponent_cards():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        # 3 losses where Mega Knight appears every time
        battles = []
        for i in range(3):
            opp = _deck("Mega Knight", "Bats", "Goblin Gang", "Skeletons",
                        "Inferno Dragon", "Zap", "Arrows", "Tornado")
            battles.append(_battle(
                f"20260410T12000{i}.000Z",
                battle_type="PvP",
                outcome_crowns=(0, 2),
                team_cards=_deck("Knight"),
                opp_cards=opp,
                deck_selection="collection",
            ))
        # 1 win for noise (should not contribute to losses)
        battles.append(_battle(
            "20260410T120004.000Z",
            battle_type="PvP",
            outcome_crowns=(2, 0),
            team_cards=_deck("Knight"),
            opp_cards=_deck("Goblin Barrel", "a", "b", "c", "d", "e", "f", "g"),
            deck_selection="collection",
        ))
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)

        out = db.get_member_recent_losses("#PLAYER", scope="ladder_ranked_10", limit=10, conn=conn)
        assert out["losses_examined"] == 3
        # Most recent battle was a win, so current loss streak is 0.
        assert out["current_loss_streak"] == 0
        names = [c["name"] for c in out["top_opponent_cards"]]
        assert "Mega Knight" in names
        mk = next(c for c in out["top_opponent_cards"] if c["name"] == "Mega Knight")
        assert mk["appearances"] == 3
        assert mk["pct_of_losses"] == 100
        # Tag exposure: opponent_tags must surface the opponent's player tag so the
        # LLM can chain into cr_api. All three losses shared #OPP.
        assert len(out["opponent_tags"]) == 1
        assert out["opponent_tags"][0]["tag"] == "#OPP"
        assert out["opponent_tags"][0]["losses_count"] == 3
    finally:
        conn.close()


def test_get_member_recent_losses_returns_empty_when_no_battles():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        out = db.get_member_recent_losses("#PLAYER", scope="war_10", conn=conn)
        assert out["losses_examined"] == 0
        assert out["top_opponent_cards"] == []
    finally:
        conn.close()


def test_get_member_recent_losses_splits_by_played_as_mode():
    """Opponent cards played as Evo/Hero aggregate separately from the same card played vanilla."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        # Two losses where the opponent played Evo Knight, one loss where Knight was vanilla
        evo_knight = _make_card("Knight", evolution_level=1)
        plain_knight = _make_card("Knight")
        battles = [
            _battle("20260410T120000.000Z", battle_type="PvP", outcome_crowns=(0, 2),
                    team_cards=_deck("Archers"),
                    opp_cards=[evo_knight] + _deck("Bats", "Skeletons", "Zap", "Arrows", "Fireball", "Tornado", "Log"),
                    deck_selection="collection"),
            _battle("20260410T120001.000Z", battle_type="PvP", outcome_crowns=(0, 2),
                    team_cards=_deck("Archers"),
                    opp_cards=[evo_knight] + _deck("Bats", "Skeletons", "Zap", "Arrows", "Fireball", "Tornado", "Log"),
                    deck_selection="collection"),
            _battle("20260410T120002.000Z", battle_type="PvP", outcome_crowns=(0, 2),
                    team_cards=_deck("Archers"),
                    opp_cards=[plain_knight] + _deck("Bats", "Skeletons", "Zap", "Arrows", "Fireball", "Tornado", "Log"),
                    deck_selection="collection"),
        ]
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)

        out = db.get_member_recent_losses("#PLAYER", scope="ladder_ranked_10", limit=10, conn=conn)
        knights = [c for c in out["top_opponent_cards"] if c["name"] == "Knight"]
        assert len(knights) == 2, "Knight should split into evo and vanilla buckets"
        evo_entry = next(c for c in knights if c.get("played_as") == "evo")
        plain_entry = next(c for c in knights if "played_as" not in c)
        assert evo_entry["appearances"] == 2
        assert plain_entry["appearances"] == 1
    finally:
        conn.close()


def test_signature_cards_split_by_played_as_mode():
    """signature_cards aggregation tags the dominant played-as mode per card so
    the LLM can say 'Evo Archers is X's signature card' specifically."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        evo_archers = _make_card("Archers", evolution_level=1)
        battles = [
            _battle("20260410T12000{}.000Z".format(i), battle_type="PvP",
                    outcome_crowns=(2, 0),
                    team_cards=[evo_archers] + _deck("Hog Rider", "Skeletons", "Zap", "Arrows", "Fireball", "Tornado", "Log"),
                    opp_cards=_deck("Knight"),
                    deck_selection="collection")
            for i in range(4)
        ]
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)

        sig = db.get_member_signature_cards("#PLAYER", mode_scope="overall", conn=conn)
        archers = next(c for c in sig["cards"] if c["name"] == "Archers")
        # The aggregation tags Archers with played_as='evo' because every play was as evo
        assert archers.get("played_as") == "evo"
        assert archers["usage_pct"] == 100
        # Non-evo cards do not carry a played_as field
        hog = next(c for c in sig["cards"] if c["name"] == "Hog Rider")
        assert "played_as" not in hog
    finally:
        conn.close()


def test_signature_cards_mixed_evo_and_vanilla_dominant_bucket_wins():
    """When a card is sometimes played as evo and sometimes vanilla, the top-N surfaces
    each variant separately by play count — a player running Evo X 80% of the time shows
    up with played_as='evo' while the vanilla bucket is a different entry with lower usage."""
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        evo_archers = _make_card("Archers", evolution_level=1)
        plain_archers = _make_card("Archers")
        # 5 battles evo Archers, 5 battles vanilla Archers — both variants should survive top-8
        # since the team deck only has one other repeating card (Log)
        battles = []
        for i in range(5):
            battles.append(_battle(f"20260410T12{i:02d}00.000Z", battle_type="PvP",
                outcome_crowns=(2,0),
                team_cards=[evo_archers, _make_card("Log"), _make_card(f"Filler{i}A"), _make_card(f"Filler{i}B"),
                            _make_card(f"Filler{i}C"), _make_card(f"Filler{i}D"), _make_card(f"Filler{i}E"),
                            _make_card(f"Filler{i}F")],
                opp_cards=_deck("Knight"),
                deck_selection="collection"))
        for i in range(5):
            battles.append(_battle(f"20260410T13{i:02d}00.000Z", battle_type="PvP",
                outcome_crowns=(2,0),
                team_cards=[plain_archers, _make_card("Log"), _make_card(f"Other{i}A"), _make_card(f"Other{i}B"),
                            _make_card(f"Other{i}C"), _make_card(f"Other{i}D"), _make_card(f"Other{i}E"),
                            _make_card(f"Other{i}F")],
                opp_cards=_deck("Knight"),
                deck_selection="collection"))
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)

        sig = db.get_member_signature_cards("#PLAYER", mode_scope="overall", conn=conn)
        archers_entries = [c for c in sig["cards"] if c["name"] == "Archers"]
        assert len(archers_entries) == 2, "Archers should split into evo and vanilla buckets"
        evo = next(c for c in archers_entries if c.get("played_as") == "evo")
        plain = next(c for c in archers_entries if "played_as" not in c)
        assert evo["usage_pct"] == 50
        assert plain["usage_pct"] == 50
    finally:
        conn.close()


# ── Phase 2b: reconstruct_member_war_decks ────────────────────────────────────

def _war_pvp_battle(battle_time, deck_names, *, outcome_crowns=(1, 0)):
    return _battle(
        battle_time,
        battle_type="riverRacePvP",
        outcome_crowns=outcome_crowns,
        team_cards=[_make_card(n) for n in deck_names],
        opp_cards=_deck("a", "b", "c", "d", "e", "f", "g", "h"),
        deck_selection="warDeckPick",
    )


def _war_duel_battle(battle_time, deck_names_per_round, *, outcome_crowns=(2, 1)):
    rounds = []
    for names in deck_names_per_round:
        rounds.append({
            "crowns": 1,
            "cards": [{**_make_card(n), "used": True} for n in names],
        })
    return {
        "type": "riverRaceDuel",
        "battleTime": battle_time,
        "gameMode": {"id": 72000267, "name": "CW_Duel_1v1"},
        "deckSelection": "warDeckPick",
        "arena": {"id": 1, "name": "Arena"},
        "team": [{
            "tag": "#PLAYER",
            "name": "Player",
            "crowns": outcome_crowns[0],
            "cards": [_make_card(n) for n in deck_names_per_round[0]],
            "supportCards": [],
            "rounds": rounds,
        }],
        "opponent": [{
            "tag": "#OPP",
            "name": "Opponent",
            "crowns": outcome_crowns[1],
            "cards": _deck("z","y","x","w","v","u","t","s"),
            "supportCards": [],
            "rounds": [{"crowns": 0, "cards": _deck("z","y","x","w","v","u","t","s")}],
        }],
    }


def test_reconstruct_war_decks_insufficient_data():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        db.snapshot_player_battlelog(
            "#PLAYER",
            [_war_pvp_battle("20260411T120000.000Z",
                             ["Knight","Archers","Cannon","Goblins","Spear Goblins","Ice Spirit","Log","Fireball"])],
            conn=conn,
        )
        out = db.reconstruct_member_war_decks("#PLAYER", conn=conn)
        assert out["status"] == "insufficient_data"
        assert out["decks"] == []
        assert any("war battle" in g.lower() for g in out["gaps"])
    finally:
        conn.close()


def test_reconstruct_war_decks_no_overlap_with_distinct_decks():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        # Build 4 distinct 8-card decks with NO shared cards (32 unique cards)
        deck1 = ["Knight","Archers","Cannon","Goblins","Spear Goblins","Ice Spirit","Log","Fireball"]
        deck2 = ["Hog Rider","Musketeer","Skeletons","Bats","Tornado","Zap","Ice Golem","Tesla"]
        deck3 = ["Giant","Witch","Wizard","Minions","Arrows","Valkyrie","Bomber","Inferno Dragon"]
        deck4 = ["Mega Knight","Bandit","Princess","Royal Ghost","Mirror","Goblin Barrel","Rocket","Mini PEKKA"]
        battles = [
            _war_pvp_battle("20260411T120000.000Z", deck1),
            _war_pvp_battle("20260411T130000.000Z", deck2),
            _war_pvp_battle("20260411T140000.000Z", deck3),
            _war_pvp_battle("20260411T150000.000Z", deck4),
        ]
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)
        out = db.reconstruct_member_war_decks("#PLAYER", conn=conn)
        assert out["status"] == "reconstructed"
        all_cards = []
        for d in out["decks"]:
            names = [c["name"] for c in d["cards"]]
            assert len(names) == 8
            all_cards.extend(names)
        assert len(all_cards) == 32
        assert len(set(all_cards)) == 32, "no-overlap regression: cards repeat across decks"
    finally:
        conn.close()


def test_reconstruct_war_decks_high_confidence_from_recent_duel():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        round1 = ["Knight","Archers","Cannon","Goblins","Spear Goblins","Ice Spirit","Log","Fireball"]
        round2 = ["Hog Rider","Musketeer","Skeletons","Bats","Tornado","Zap","Ice Golem","Tesla"]
        round3 = ["Giant","Witch","Wizard","Minions","Arrows","Valkyrie","Bomber","Inferno Dragon"]
        deck4 = ["Mega Knight","Bandit","Princess","Royal Ghost","Mirror","Goblin Barrel","Rocket","Mini PEKKA"]
        battles = [
            _war_pvp_battle("20260411T100000.000Z", deck4),
            _war_duel_battle("20260411T120000.000Z", [round1, round2, round3]),
        ]
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)
        out = db.reconstruct_member_war_decks("#PLAYER", conn=conn)
        assert out["status"] == "reconstructed"
        assert out["confidence"] == "high"
    finally:
        conn.close()


def test_reconstruct_war_decks_partial_when_under_4_distinct():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members([{"tag": "#PLAYER", "name": "Player", "role": "member"}], conn=conn)
        deck1 = ["Knight","Archers","Cannon","Goblins","Spear Goblins","Ice Spirit","Log","Fireball"]
        deck2 = ["Hog Rider","Musketeer","Skeletons","Bats","Tornado","Zap","Ice Golem","Tesla"]
        battles = [
            _war_pvp_battle("20260411T120000.000Z", deck1),
            _war_pvp_battle("20260411T130000.000Z", deck1),  # same deck, repeated
            _war_pvp_battle("20260411T140000.000Z", deck2),
        ]
        db.snapshot_player_battlelog("#PLAYER", battles, conn=conn)
        out = db.reconstruct_member_war_decks("#PLAYER", conn=conn)
        assert out["status"] == "partial"
        assert len(out["decks"]) == 2
        assert any("4 war decks" in g for g in out["gaps"])
    finally:
        conn.close()


# ── Phase 5: war deck suggestion validator ────────────────────────────────────

def test_validate_war_deck_suggestion_accepts_4_decks_of_8_unique_cards():
    from agent.workflows import _validate_war_deck_suggestion
    decks = [[f"d{d}c{c}" for c in range(8)] for d in range(4)]
    assert _validate_war_deck_suggestion({"proposed_decks": decks}) is None


def test_validate_war_deck_suggestion_rejects_overlap():
    from agent.workflows import _validate_war_deck_suggestion
    decks = [[f"d{d}c{c}" for c in range(8)] for d in range(4)]
    decks[1][0] = "d0c0"  # duplicate from deck 0
    error = _validate_war_deck_suggestion({"proposed_decks": decks})
    assert error and "no-overlap" in error and "d0c0" in error


def test_validate_war_deck_suggestion_rejects_missing_field():
    from agent.workflows import _validate_war_deck_suggestion
    error = _validate_war_deck_suggestion({})
    assert error and "exactly 4" in error


def test_validate_war_deck_suggestion_rejects_short_deck():
    from agent.workflows import _validate_war_deck_suggestion
    decks = [[f"d{d}c{c}" for c in range(8)] for d in range(4)]
    decks[2] = decks[2][:7]  # only 7 cards in deck 3
    error = _validate_war_deck_suggestion({"proposed_decks": decks})
    assert error and "exactly 8" in error


# ── New-war-player flow: war review with no reconstructable decks ─────────────

def test_respond_in_deck_review_war_review_for_new_player_injects_offer_instruction():
    """When mode=war + subject=review + status=insufficient_data, the user_msg
    sent to the LLM must include the explicit new-player offer instruction so
    the response reliably invites the user to switch into suggest mode."""
    from unittest.mock import patch
    from agent import workflows

    captured = {}

    def fake_chat(system_prompt, user_msg, **kwargs):
        captured["user_msg"] = user_msg
        return {
            "event_type": "deck_review_response",
            "summary": "ok",
            "content": "Reply `build my war decks` and I'll put together a starter kit.",
        }

    fake_war_decks = {
        "status": "insufficient_data",
        "member_tag": "#NEW",
        "member_name": "NewWarPlayer",
        "decks": [],
        "evidence": {"war_battles_seen": 0, "distinct_decks_observed": 0},
        "gaps": ["No war battles."],
        "guidance": "Offer to build decks.",
    }

    with patch.object(workflows, "_chat_with_tools", side_effect=fake_chat), \
         patch.object(workflows.db, "reconstruct_member_war_decks", return_value=fake_war_decks):
        result = workflows.respond_in_deck_review(
            question="review my war decks",
            author_name="someone",
            channel_name="#ask-elixir",
            mode="war",
            subject="review",
            target_member_tag="#NEW",
            target_member_name="NewWarPlayer",
        )

    assert result["event_type"] == "deck_review_response"
    msg = captured["user_msg"]
    assert "PRE-FETCHED WAR DECK RECONSTRUCTION" in msg
    assert "insufficient_data" in msg
    assert "NEW WAR PLAYER" in msg
    assert "build my war decks" in msg


def test_respond_in_deck_review_injects_full_card_collection_with_levels():
    """The 2026-04-24 hallucination — Elixir saying Fireball was L12 when it
    was actually L8 — happened because the LLM never fetched the card
    collection and made up levels for swap candidates. We now inject the
    owned collection (name, level, rarity, count) into the user message with
    a strong "use ONLY these levels" directive so the model can't invent."""
    from unittest.mock import patch
    from agent import workflows

    captured = {}

    def fake_chat(system_prompt, user_msg, **kwargs):
        captured["user_msg"] = user_msg
        return {"event_type": "deck_review_response", "summary": "ok", "content": "ok"}

    fake_collection = {
        "cards": [
            {"name": "Royal Ghost", "level": 12, "rarity": "legendary", "count": 6},
            {"name": "Fireball", "level": 8, "rarity": "rare", "count": 102},
            {"name": "Inferno Dragon", "level": 9, "rarity": "legendary", "count": 1},
        ],
        "support_cards": [
            {"name": "Tower Princess", "level": 14, "rarity": "legendary", "count": 30},
        ],
    }

    with patch.object(workflows, "_chat_with_tools", side_effect=fake_chat), \
         patch.object(workflows.db, "get_member_current_deck", return_value=None), \
         patch.object(workflows.db, "get_member_card_collection", return_value=fake_collection):
        workflows.respond_in_deck_review(
            question="review my deck",
            author_name="shimmeringhost",
            channel_name="#ask-elixir",
            mode="regular",
            subject="review",
            target_member_tag="#2209PJPVGG",
            target_member_name="shimmeringhost",
        )

    msg = captured["user_msg"]
    assert "YOUR CARD COLLECTION" in msg
    # Authoritative levels for the exact cards involved in the hallucination.
    assert "Fireball: L8 rare" in msg
    assert "Inferno Dragon: L9 legendary" in msg
    assert "Royal Ghost: L12 legendary" in msg
    # Support cards are included.
    assert "Tower Princess: L14 legendary" in msg
    # Total count is reported.
    assert "Total: 4 cards owned" in msg
    # The directive that prevents hallucination is present.
    assert "AUTHORITATIVE levels" in msg
    assert "Never invent or infer" in msg


def test_respond_in_deck_review_war_review_with_decks_does_not_inject_new_player_instruction():
    """Sanity check: when war_decks reconstruction succeeds, the special new-player
    instruction is NOT injected (only the pre-fetch context is)."""
    from unittest.mock import patch
    from agent import workflows

    captured = {}

    def fake_chat(system_prompt, user_msg, **kwargs):
        captured["user_msg"] = user_msg
        return {"event_type": "deck_review_response", "summary": "ok", "content": "ok"}

    fake_war_decks = {
        "status": "reconstructed",
        "confidence": "high",
        "decks": [{"deck_index": i, "cards": []} for i in range(1, 5)],
        "evidence": {},
        "gaps": [],
        "guidance": "",
    }

    with patch.object(workflows, "_chat_with_tools", side_effect=fake_chat), \
         patch.object(workflows.db, "reconstruct_member_war_decks", return_value=fake_war_decks):
        workflows.respond_in_deck_review(
            question="review my war decks",
            author_name="someone",
            channel_name="#ask-elixir",
            mode="war",
            subject="review",
            target_member_tag="#ACTIVE",
            target_member_name="ActiveWarPlayer",
        )

    msg = captured["user_msg"]
    assert "PRE-FETCHED WAR DECK RECONSTRUCTION" in msg
    assert "NEW WAR PLAYER" not in msg


