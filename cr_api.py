"""Clash Royale API client."""
import os
import requests
from dotenv import load_dotenv

import prompts

load_dotenv()

API_BASE = "https://api.clashroyale.com/v1"
CLAN_TAG = prompts.clan_tag()
API_KEY = os.getenv("CR_API_KEY", "")


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def get_clan():
    url = f"{API_BASE}/clans/%23{CLAN_TAG}"
    r = requests.get(url, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_current_war():
    url = f"{API_BASE}/clans/%23{CLAN_TAG}/currentriverrace"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_river_race_log():
    url = f"{API_BASE}/clans/%23{CLAN_TAG}/riverracelog"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_player(tag):
    """Fetch individual player profile from CR API.

    tag: player tag with or without '#' prefix.
    Returns player dict or None on error.
    """
    clean_tag = tag.lstrip("#")
    url = f"{API_BASE}/players/%23{clean_tag}"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_player_battle_log(tag):
    """Fetch a player's recent battle log (~25 battles).

    tag: player tag with or without '#' prefix.
    Returns list of battle objects or None on error.
    """
    clean_tag = tag.lstrip("#")
    url = f"{API_BASE}/players/%23{clean_tag}/battlelog"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_player_chests(tag):
    """Fetch a player's upcoming chest cycle.

    tag: player tag with or without '#' prefix.
    Returns list of chest objects or None on error.
    """
    clean_tag = tag.lstrip("#")
    url = f"{API_BASE}/players/%23{clean_tag}/upcomingchests"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception:
        return None
