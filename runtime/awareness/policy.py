"""Deterministic policy for proactive member-facing copy.

Prompts express Elixir's editorial judgment; code owns invariants. The model has
already demonstrated that a rule buried in a long prompt can be ignored, so no
awareness post reaches Discord until this module accepts the complete plan.
"""

from __future__ import annotations

import re
from collections import Counter

from capabilities.game_truth import awareness_post_facts
from engine.game_check import check_post

_GENDERED_MEMBER_PRONOUN = re.compile(
    r"\b(?:he|him|his|himself|she|her|hers|herself)\b",
    re.IGNORECASE,
)

_UNRANKED_CURRENT_RANK = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btoday(?:'s|’s)?\s+rank\s*#?\d+\b",
        r"\b(?:currently|right\s+now|now)\s+(?:sitting\s+)?(?:at\s+|in\s+)?"
        r"(?:rank(?:ed)?\s*|place\s*|#)\d+\b",
        r"\bsitting\s+(?:at|in)\s+(?:rank\s*)?#?\d+(?:st|nd|rd|th)?\b",
        r"\b(?:rank|place)\s*#?\d+\s+(?:today|currently|right\s+now)\b",
        r"\bwe(?:'re|’re|\s+are)\s+(?:currently\s+)?(?:sitting\s+)?"
        r"(?:(?:at|in)\s+)?(?:#?\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth)"
        r"(?:\s+place)?\b",
        r"\b(?:currently|right\s+now|now)\s+we(?:'re|’re|\s+are)\s+"
        r"(?:sitting\s+)?(?:(?:at|in)\s+)?"
        r"(?:#?\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth)"
        r"(?:\s+place)?\b",
    )
)

_NUMBER = re.compile(r"(?<!\w)[+-]?\d[\d,]*(?:\.\d+)?%?(?!\w)")


def _post_text(post: dict) -> str:
    values = [post.get("content"), post.get("clan_chat")]
    chunks: list[str] = []
    for value in values:
        if isinstance(value, list):
            chunks.extend(str(item) for item in value if item is not None)
        elif value is not None:
            chunks.append(str(value))
    return "\n".join(chunks)


def _race_is_explicitly_unranked(read: dict) -> bool:
    candidates = [
        (read.get("standing") or {}).get("race_ranked"),
        (read.get("war_season") or {}).get("race_ranked"),
        (((read.get("war_season") or {}).get("state") or {}).get("race") or {}).get(
            "race_ranked"
        ),
    ]
    return any(value is False for value in candidates)


def apply_editorial_admission(read: dict, plan: dict) -> tuple[dict, list[dict]]:
    """Drop only high-confidence routine repeats before they reach Discord.

    The awareness prompt already carries a 48-hour per-member cooldown, but a
    prompt is not an enforcement boundary.  This admission stays deliberately
    narrow: it only suppresses a one-signal, one-member milestone/clan-event
    post when that member was recently solo-highlighted.  Hard-post coverage,
    notable signals, and multi-signal roundups are never changed here.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("posts"), list):
        return plan, []

    recent = {
        str(item.get("member_tag")): item
        for item in (read.get("recent_member_spotlights") or [])
        if isinstance(item, dict) and item.get("member_tag") and item.get("solo")
    }
    hard_keys = {
        str(signal.get("signal_key"))
        for signal in (read.get("hard_post_signals") or [])
        if isinstance(signal, dict) and signal.get("signal_key")
    }
    admitted: list[dict] = []
    suppressed: list[dict] = []
    for post in plan["posts"]:
        if not isinstance(post, dict):
            admitted.append(post)
            continue
        tags = [str(tag) for tag in (post.get("member_tags") or []) if tag]
        covers = [str(key) for key in (post.get("covers_signal_keys") or []) if key]
        facts = awareness_post_facts(read, post)
        is_routine_repeat = (
            post.get("leads_with") in {"milestone", "clan_event"}
            and len(tags) == 1
            and tags[0] in recent
            and len(facts.get("covered_signals") or []) == 1
            and not facts.get("notable_moment")
            and not hard_keys.intersection(covers)
        )
        if not is_routine_repeat:
            admitted.append(post)
            continue
        suppressed.append(
            {
                "member_tag": tags[0],
                "summary": post.get("summary"),
                "covers_signal_keys": covers,
                "prior_spotlight": recent[tags[0]],
            }
        )

    if not suppressed:
        return plan, []
    plan["posts"] = admitted
    plan["_editorial_suppressed"] = suppressed
    if not admitted:
        plan["skipped_reason"] = (
            "Editorial admission suppressed a routine repeat inside the "
            "48-hour member spotlight cooldown."
        )
    return plan, suppressed


def validate_plan(read: dict, plan: dict) -> list[str]:
    """Return stable violation codes for a complete awareness post plan."""
    if not isinstance(plan, dict):
        return ["plan.invalid_shape"]
    posts = plan.get("posts") or []
    if not isinstance(posts, list):
        return ["plan.posts.invalid_shape"]
    violations: list[str] = []
    unranked = _race_is_explicitly_unranked(read or {})
    for index, post in enumerate(posts):
        if not isinstance(post, dict):
            violations.append(f"post[{index}].invalid_shape")
            continue
        text = _post_text(post)
        if _GENDERED_MEMBER_PRONOUN.search(text):
            violations.append(f"post[{index}].gendered_member_pronoun")
        if unranked and post.get("leads_with") == "war":
            if any(pattern.search(text) for pattern in _UNRANKED_CURRENT_RANK):
                violations.append(f"post[{index}].current_rank_while_unranked")
        for finding in check_post(text, awareness_post_facts(read or {}, post)):
            issue = str(finding.get("issue") or "contradiction").lower()
            if "colosseum" in issue or "every colosseum battle" in issue:
                code = "colosseum_mechanics"
            elif "impossible war day" in issue:
                code = "war_day"
            elif "card" in issue or "level" in issue:
                code = "card_mechanics"
            else:
                code = "game_truth"
            violations.append(f"post[{index}].{code}")
    return violations


def validate_repair(original: dict, repaired: dict) -> list[str]:
    """A repair may rewrite copy, never decisions or evidence coverage."""
    if not isinstance(original, dict) or not isinstance(repaired, dict):
        return ["repair.invalid_shape"]
    original_posts = (original or {}).get("posts") or []
    repaired_posts = (repaired or {}).get("posts") or []
    if len(original_posts) != len(repaired_posts):
        return ["repair.changed_post_count"]
    mutable = {"content", "clan_chat", "summary", "relay_reason"}
    violations: list[str] = []
    for key in sorted((set(original) | set(repaired)) - {"posts"}):
        if original.get(key) != repaired.get(key):
            violations.append(f"repair.changed_{key}")
    for index, (before, after) in enumerate(
        zip(original_posts, repaired_posts, strict=True)
    ):
        if not isinstance(before, dict) or not isinstance(after, dict):
            violations.append(f"repair.post[{index}].invalid_shape")
            continue
        keys = (set(before) | set(after)) - mutable
        for key in sorted(keys):
            if before.get(key) != after.get(key):
                violations.append(f"repair.post[{index}].changed_{key}")
        before_text = _post_text(before)
        after_text = _post_text(after)
        before_numbers = Counter(_NUMBER.findall(before_text))
        after_numbers = Counter(_NUMBER.findall(after_text))
        if after_numbers - before_numbers:
            violations.append(f"repair.post[{index}].introduced_number")

        # The only factual number a repair may remove is the invalid CURRENT
        # race rank that triggered this policy. Preserve every other number
        # byte-for-byte, including historical streaks in the same post.
        removable: Counter[str] = Counter()
        for pattern in _UNRANKED_CURRENT_RANK:
            for match in pattern.finditer(before_text):
                removable.update(_NUMBER.findall(match.group(0)))
        required = before_numbers - removable
        if required - after_numbers:
            violations.append(f"repair.post[{index}].removed_factual_number")
    return violations


__all__ = ["apply_editorial_admission", "validate_plan", "validate_repair"]
