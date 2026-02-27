"""Clash Royale API client."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.clashroyale.com/v1"
CLAN_TAG = os.getenv("CR_CLAN_TAG", "J2RGCRVG")
API_KEY = os.getenv("CR_API_KEY", "")


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def get_clan():
    url = f"{API_BASE}/clans/%23{CLAN_TAG}"
    r = requests.get(url, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_current_war():
    # Try River Race first (current format), fall back to legacy war endpoint
    for endpoint in ["currentriverrace", "currentwar"]:
        url = f"{API_BASE}/clans/%23{CLAN_TAG}/{endpoint}"
        try:
            r = requests.get(url, headers=_headers(), timeout=10)
            r.raise_for_status()
            data = r.json()
            if data:
                return data
        except Exception:
            continue
    return None


def get_river_race_log():
    url = f"{API_BASE}/clans/%23{CLAN_TAG}/riverracelog"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None
