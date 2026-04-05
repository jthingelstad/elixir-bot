"""Clash Royale API client."""
import os
import time
import requests
from dotenv import load_dotenv

import prompts
from runtime import status as runtime_status

load_dotenv()

API_BASE = "https://api.clashroyale.com/v1"
CLAN_TAG = prompts.clan_tag()
API_KEY = os.getenv("CR_API_KEY", "")


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def _request_json(endpoint_path, *, endpoint_name, entity_key=None):
    url = f"{API_BASE}{endpoint_path}"
    started = time.perf_counter()
    try:
        response = requests.get(url, headers=_headers(), timeout=10)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.raise_for_status()
        runtime_status.record_api_call(
            endpoint_name,
            entity_key,
            ok=True,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response.json()
    except Exception as exc:
        response = locals().get("response")
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        runtime_status.record_api_call(
            endpoint_name,
            entity_key,
            ok=False,
            status_code=getattr(response, "status_code", None),
            error=exc,
            duration_ms=duration_ms,
        )
        raise


def get_clan():
    return _request_json(f"/clans/%23{CLAN_TAG}", endpoint_name="clan", entity_key=CLAN_TAG)


def get_current_war():
    try:
        return _request_json(
            f"/clans/%23{CLAN_TAG}/currentriverrace",
            endpoint_name="currentriverrace",
            entity_key=CLAN_TAG,
        )
    except Exception:
        return None


def get_river_race_log():
    try:
        return _request_json(
            f"/clans/%23{CLAN_TAG}/riverracelog",
            endpoint_name="riverracelog",
            entity_key=CLAN_TAG,
        )
    except Exception:
        return None


def get_player(tag):
    """Fetch individual player profile from CR API.

    tag: player tag with or without '#' prefix.
    Returns player dict or None on error.
    """
    clean_tag = tag.lstrip("#")
    try:
        return _request_json(
            f"/players/%23{clean_tag}",
            endpoint_name="player",
            entity_key=clean_tag.upper(),
        )
    except Exception:
        return None


def get_player_battle_log(tag):
    """Fetch a player's recent battle log.

    tag: player tag with or without '#' prefix.
    Returns list of battle objects or None on error.
    """
    clean_tag = tag.lstrip("#")
    try:
        return _request_json(
            f"/players/%23{clean_tag}/battlelog",
            endpoint_name="player_battlelog",
            entity_key=clean_tag.upper(),
        )
    except Exception:
        return None


def get_tournament(tag):
    """Fetch tournament details by tag.

    tag: tournament tag with or without '#' prefix.
    Returns tournament dict or None on error.
    """
    clean_tag = tag.lstrip("#")
    try:
        return _request_json(
            f"/tournaments/%23{clean_tag}",
            endpoint_name="tournament",
            entity_key=clean_tag.upper(),
        )
    except Exception:
        return None


def get_player_chests(tag):
    """Fetch a player's upcoming chest cycle.

    tag: player tag with or without '#' prefix.
    Returns list of chest objects or None on error.
    """
    clean_tag = tag.lstrip("#")
    try:
        payload = _request_json(
            f"/players/%23{clean_tag}/upcomingchests",
            endpoint_name="player_chests",
            entity_key=clean_tag.upper(),
        )
        return payload.get("items", [])
    except Exception:
        return None
