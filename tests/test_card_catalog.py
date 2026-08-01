"""Tests for the Clash Royale card catalog storage layer."""

import pytest

import db
from storage.card_catalog import _escape_like, lookup_cards, sync_card_catalog


@pytest.fixture
def catalog_db():
    conn = db.get_connection(":memory:")
    yield conn
    conn.close()


_SAMPLE_API_RESPONSE = {
    "items": [
        {
            "id": 26000000,
            "name": "Knight",
            "elixirCost": 3,
            "rarity": "Common",
            "maxLevel": 15,
            "maxEvolutionLevel": 1,
            "iconUrls": {"medium": "https://example.com/knight.png"},
        },
        {
            "id": 26000001,
            "name": "Archers",
            "elixirCost": 3,
            "rarity": "Common",
            "maxLevel": 15,
            "iconUrls": {"medium": "https://example.com/archers.png"},
        },
        {
            "id": 26000002,
            "name": "Giant",
            "elixirCost": 5,
            "rarity": "Rare",
            "maxLevel": 13,
            "iconUrls": {"medium": "https://example.com/giant.png"},
        },
        {
            "id": 26000003,
            "name": "P.E.K.K.A",
            "elixirCost": 7,
            "rarity": "Epic",
            "maxLevel": 11,
            "iconUrls": {"medium": "https://example.com/pekka.png"},
        },
        {
            "id": 26000004,
            "name": "Balloon",
            "elixirCost": 5,
            "rarity": "Epic",
            "maxLevel": 11,
            "iconUrls": {"medium": "https://example.com/balloon.png"},
        },
    ],
}


def _seed_catalog(conn):
    sync_card_catalog(_SAMPLE_API_RESPONSE, conn=conn)


class TestRarityFilterGuard:
    def test_unknown_rarity_returns_none(self):
        # QA L17: 'mythic' must normalize to None so the unknown_rarity guard
        # fires instead of a valid-looking 0-match result.
        from storage.cards import _normalize_rarity_filter

        assert _normalize_rarity_filter("mythic") is None
        assert _normalize_rarity_filter("legendaries") == "legendary"
        assert _normalize_rarity_filter("Champion") == "champion"


class TestLikeEscape:
    def test_escape_percent(self):
        assert _escape_like("50%") == "50\\%"

    def test_escape_underscore(self):
        assert _escape_like("P_E_K_K_A") == "P\\_E\\_K\\_K\\_A"

    def test_escape_backslash(self):
        assert _escape_like("a\\b") == "a\\\\b"

    def test_no_escape_needed(self):
        assert _escape_like("Knight") == "Knight"


class TestCardCatalogLookup:
    def test_lookup_by_name(self, catalog_db):
        _seed_catalog(catalog_db)
        results = lookup_cards(name="Knight", conn=catalog_db)["cards"]
        # QA M17: an exact-name match ranks first, not alphabetically.
        assert results[0]["name"] == "Knight"

    def test_lookup_wildcards_in_name_do_not_expand(self, catalog_db):
        _seed_catalog(catalog_db)
        out = lookup_cards(name="%", conn=catalog_db)
        assert out["cards"] == [] and out["total_matched"] == 0

    def test_lookup_by_rarity(self, catalog_db):
        _seed_catalog(catalog_db)
        results = lookup_cards(rarity="epic", conn=catalog_db)["cards"]
        assert all(r["rarity"] == "epic" for r in results)
        assert {r["name"] for r in results} == {"Balloon", "P.E.K.K.A"}

    def test_lookup_surfaces_truncation(self, catalog_db):
        _seed_catalog(catalog_db)
        out = lookup_cards(limit=2, conn=catalog_db)
        # QA M18: a capped result reports total_matched + truncated.
        assert out["returned"] == 2
        assert out["total_matched"] > 2
        assert out["truncated"] is True

    def test_capability_booleans(self, catalog_db):
        # QA L16: Knight has an Evo form (maxEvolutionLevel=1) — supports_evo True,
        # supports_hero False; the label reads as a capability, not an unlock.
        _seed_catalog(catalog_db)
        knight = lookup_cards(name="Knight", conn=catalog_db)["cards"][0]
        assert knight["supports_evo"] is True
        assert knight["supports_hero"] is False
        assert "available" in knight["mode_label"]
        archers = lookup_cards(name="Archers", conn=catalog_db)["cards"][0]
        assert archers["supports_evo"] is False
        assert archers["mode_label"] is None

    def test_cost_filter_notes_null_cost_exclusion(self, catalog_db):
        # QA L15: a min/max_cost filter drops NULL-cost cards — surface that.
        _seed_catalog(catalog_db)
        out = lookup_cards(min_cost=0, max_cost=10, conn=catalog_db)
        assert "cost_filter_note" in out
        # No cost filter → no note.
        assert "cost_filter_note" not in lookup_cards(name="Knight", conn=catalog_db)


@pytest.fixture
def roles_db(catalog_db):
    """A catalog plus the enriched role facts the tools read."""
    cards = [
        (26000021, "Hog Rider", "win_condition"),
        (26000006, "Balloon", "win_condition"),
        (26000005, "Goblin Barrel", "win_condition"),
        (26000004, "P.E.K.K.A", "tank"),
        (26000014, "Musketeer", "support"),
        (28000001, "Arrows", "spell"),
    ]
    for card_id, name, role in cards:
        catalog_db.execute(
            "INSERT INTO card_catalog (card_id, name, elixir_cost, rarity, max_level, "
            "card_type, synced_at) VALUES (?, ?, 4, 'common', 14, 'troop', '2026-08-01')",
            (card_id, name),
        )
        catalog_db.execute(
            "INSERT INTO card_facts (card_id, evolution_level, role) VALUES (?, 0, ?)",
            (card_id, role),
        )
    catalog_db.commit()
    return catalog_db


def test_cards_can_be_looked_up_by_what_they_are_for(roles_db):
    """A member asked "what cards are win cons?" and got the two in the deck he had
    just been shown — because nothing could ask the catalog that question. The role
    facts had been enriched for 122 cards and no tool could filter on them."""
    wincons = lookup_cards(role="win_condition", limit=50, conn=roles_db)
    names = {c["name"] for c in wincons["cards"]}
    assert names == {"Hog Rider", "Balloon", "Goblin Barrel"}
    assert "Musketeer" not in names, "support is not a win condition"

    tanks = {c["name"] for c in lookup_cards(role="tank", limit=50, conn=roles_db)["cards"]}
    assert tanks == {"P.E.K.K.A"}
    assert tanks.isdisjoint(names), "a tank and a win condition are different slots"


def test_role_lookup_accepts_the_words_members_actually_use(roles_db):
    """ "win cons" is what a person types. Demanding the exact enum returns zero
    cards, which reads as "there are none" rather than "say it differently"."""
    canonical = lookup_cards(role="win_condition", limit=50, conn=roles_db)["total_matched"]
    assert canonical == 3
    for spoken in ("win cons", "wincons", "Win Conditions", "win-condition"):
        got = lookup_cards(role=spoken, limit=50, conn=roles_db)["total_matched"]
        assert got == canonical, spoken
    assert lookup_cards(role="spells", limit=50, conn=roles_db)["total_matched"] == 1


def test_an_unknown_role_returns_nothing_rather_than_everything(roles_db):
    assert lookup_cards(role="nonsense", limit=50, conn=roles_db)["total_matched"] == 0
