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

_VALID_TAG_CHARS = frozenset("0289PYLQGRJCUV")


class InvalidTagError(ValueError):
    """Raised when a string is not a valid Clash Royale tag."""


def _strip_tag(raw) -> str:
    """Permissive: strip whitespace, drop leading #, uppercase.

    Used by internal API fetchers. Raises InvalidTagError only on empty input.
    """
    if raw is None:
        raise InvalidTagError("tag is required")
    cleaned = str(raw).strip().lstrip("#").upper()
    if not cleaned:
        raise InvalidTagError("tag is empty")
    return cleaned


def _normalize_cr_tag(raw) -> str:
    """Strict: strip + uppercase + validate the CR alphabet.

    Returns the canonical tag (no # prefix). Raises InvalidTagError on empty
    input or characters outside the Clash Royale alphabet (0, 2, 8, 9 and
    P, Y, L, Q, G, R, J, C, U, V). Intended for LLM-facing tool handlers
    where structured rejection is preferable to a 404 from the API.
    """
    cleaned = _strip_tag(raw)
    bad = set(cleaned) - _VALID_TAG_CHARS
    if bad:
        raise InvalidTagError(
            f"invalid characters in tag '{cleaned}': {''.join(sorted(bad))}"
        )
    return cleaned


_TTL_CACHE: dict[tuple[str, str], tuple[float, object]] = {}


def _cache_get(key: tuple[str, str]):
    entry = _TTL_CACHE.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if time.time() >= expires_at:
        _TTL_CACHE.pop(key, None)
        return None
    log.debug("cr_api cache hit key=%s", key)
    return payload


def _cache_set(key: tuple[str, str], payload, ttl_seconds: float) -> None:
    _TTL_CACHE[key] = (time.time() + ttl_seconds, payload)


def _cache_clear() -> None:
    """Drop all cached entries. Intended for tests."""
    _TTL_CACHE.clear()


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


def _cached_fetch(endpoint_name, tag, path, ttl_seconds):
    """Fetch with TTL cache. Returns payload or None on error.

    Cache hits skip record_api_call (the call didn't happen). Misses fetch
    via _request_json, record metrics as normal, and populate the cache.
    """
    cache_key = (endpoint_name, tag)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        payload = _request_json(path, endpoint_name=endpoint_name, entity_key=tag)
    except requests.RequestException:
        return None
    _cache_set(cache_key, payload, ttl_seconds)
    return payload


def get_clan():
    return _request_json(f"/clans/%23{CLAN_TAG}", endpoint_name="clan", entity_key=CLAN_TAG)


def get_current_war(tag=None):
    """Fetch the current river race for a clan.

    tag: clan tag (with or without '#'); defaults to our own clan.
    """
    clean_tag = _strip_tag(tag) if tag else CLAN_TAG
    return _cached_fetch(
        "currentriverrace", clean_tag,
        f"/clans/%23{clean_tag}/currentriverrace",
        ttl_seconds=90,
    )


def get_river_race_log(tag=None):
    """Fetch the historical river race log for a clan.

    tag: clan tag (with or without '#'); defaults to our own clan.
    """
    clean_tag = _strip_tag(tag) if tag else CLAN_TAG
    return _cached_fetch(
        "riverracelog", clean_tag,
        f"/clans/%23{clean_tag}/riverracelog",
        ttl_seconds=600,
    )


def get_player(tag):
    """Fetch individual player profile from CR API."""
    clean_tag = _strip_tag(tag)
    return _cached_fetch(
        "player", clean_tag,
        f"/players/%23{clean_tag}",
        ttl_seconds=90,
    )


def get_player_battle_log(tag):
    """Fetch a player's recent battle log."""
    clean_tag = _strip_tag(tag)
    return _cached_fetch(
        "player_battlelog", clean_tag,
        f"/players/%23{clean_tag}/battlelog",
        ttl_seconds=60,
    )


def get_tournament(tag):
    """Fetch tournament details by tag."""
    clean_tag = _strip_tag(tag)
    return _cached_fetch(
        "tournament", clean_tag,
        f"/tournaments/%23{clean_tag}",
        ttl_seconds=300,
    )


def get_player_chests(tag):
    """Fetch a player's upcoming chest cycle.

    Returns list of chest dicts or None on error.
    """
    clean_tag = _strip_tag(tag)
    payload = _cached_fetch(
        "player_chests", clean_tag,
        f"/players/%23{clean_tag}/upcomingchests",
        ttl_seconds=300,
    )
    if payload is None:
        return None
    return payload.get("items", [])


def get_clan_by_tag(tag):
    """Fetch any clan's profile by tag."""
    clean_tag = _strip_tag(tag)
    return _cached_fetch(
        "clan_by_tag", clean_tag,
        f"/clans/%23{clean_tag}",
        ttl_seconds=120,
    )


def get_cards():
    """Fetch the full card catalog from the Clash Royale API.

    Returns dict with 'items' (121 standard cards) and 'supportItems'
    (4 Tower Troops), or None on error.
    """
    try:
        return _request_json("/cards", endpoint_name="cards")
    except requests.RequestException:
        return None
