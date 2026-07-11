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
