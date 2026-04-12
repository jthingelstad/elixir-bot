"""Clash Royale API client."""
import logging
import os
import random
import time
import requests
from dotenv import load_dotenv

import prompts
from runtime import status as runtime_status

load_dotenv()

log = logging.getLogger(__name__)

API_BASE = "https://api.clashroyale.com/v1"
CLAN_TAG = prompts.clan_tag()
API_KEY = os.getenv("CR_API_KEY", "")

_MAX_RETRIES = 2
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def _elapsed_ms(started):
    return round((time.perf_counter() - started) * 1000, 2)


def _is_transient_status(status_code: int | None) -> bool:
    """429 (rate limit) and 5xx are considered transient and retried."""
    if status_code is None:
        return False
    return status_code == 429 or 500 <= status_code < 600


def _retry_delay(attempt: int, response: "requests.Response | None") -> float:
    """Exponential backoff with jitter, honouring Retry-After if present."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _RETRY_MAX_SECONDS)
            except (TypeError, ValueError):
                pass
    # 1, 2, 4, 8, … with up to 50% jitter.
    base = min(_RETRY_BASE_SECONDS * (2 ** attempt), _RETRY_MAX_SECONDS)
    return base + random.uniform(0, base * 0.5)


def _request_json(endpoint_path, *, endpoint_name, entity_key=None):
    url = f"{API_BASE}{endpoint_path}"
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        started = time.perf_counter()
        response = None
        try:
            response = requests.get(url, headers=_headers(), timeout=10)
            response.raise_for_status()
            runtime_status.record_api_call(
                endpoint_name,
                entity_key,
                ok=True,
                status_code=response.status_code,
                duration_ms=_elapsed_ms(started),
            )
            if attempt > 0:
                log.info(
                    "api_retry_success endpoint=%s attempt=%d/%d duration_ms=%.1f",
                    endpoint_name, attempt + 1, _MAX_RETRIES + 1, _elapsed_ms(started),
                )
            return response.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            status_code = None
            log.warning("CR API %s on %s (attempt %d/%d): %s",
                        type(exc).__name__, endpoint_name, attempt + 1, _MAX_RETRIES + 1, exc)
            runtime_status.record_api_call(
                endpoint_name, entity_key, ok=False,
                status_code=status_code, error=exc, duration_ms=_elapsed_ms(started),
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_retry_delay(attempt, None))
                continue
        except requests.HTTPError as exc:
            last_exc = exc
            status_code = response.status_code if response is not None else None
            log.warning("CR API HTTP %s on %s (attempt %d/%d): %s",
                        status_code, endpoint_name, attempt + 1, _MAX_RETRIES + 1, exc)
            runtime_status.record_api_call(
                endpoint_name, entity_key, ok=False,
                status_code=status_code, error=exc, duration_ms=_elapsed_ms(started),
            )
            if _is_transient_status(status_code) and attempt < _MAX_RETRIES:
                time.sleep(_retry_delay(attempt, response))
                continue
            raise
        except (requests.RequestException, ValueError) as exc:
            log.warning("CR API error on %s: %s", endpoint_name, exc)
            runtime_status.record_api_call(
                endpoint_name, entity_key, ok=False,
                status_code=getattr(response, "status_code", None),
                error=exc, duration_ms=_elapsed_ms(started),
            )
            raise
    raise last_exc


def get_clan():
    return _request_json(f"/clans/%23{CLAN_TAG}", endpoint_name="clan", entity_key=CLAN_TAG)


def get_current_war():
    try:
        return _request_json(
            f"/clans/%23{CLAN_TAG}/currentriverrace",
            endpoint_name="currentriverrace",
            entity_key=CLAN_TAG,
        )
    except requests.RequestException:
        return None


def get_river_race_log():
    try:
        return _request_json(
            f"/clans/%23{CLAN_TAG}/riverracelog",
            endpoint_name="riverracelog",
            entity_key=CLAN_TAG,
        )
    except requests.RequestException:
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
    except requests.RequestException:
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
    except requests.RequestException:
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
    except requests.RequestException:
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
    except requests.RequestException:
        return None


def get_clan_by_tag(tag):
    """Fetch any clan's profile by tag.

    tag: clan tag with or without '#' prefix.
    Returns clan dict or None on error.
    """
    clean_tag = tag.lstrip("#")
    try:
        return _request_json(
            f"/clans/%23{clean_tag}",
            endpoint_name="clan_by_tag",
            entity_key=clean_tag.upper(),
        )
    except requests.RequestException:
        return None


def get_cards():
    """Fetch the full card catalog from the Clash Royale API.

    Returns dict with 'items' (121 standard cards) and 'supportItems'
    (4 Tower Troops), or None on error.
    """
    try:
        return _request_json("/cards", endpoint_name="cards")
    except requests.RequestException:
        return None
