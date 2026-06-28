"""Discord rendering + posters for communication intents.

`render_intent` turns an intent's presentation-free `summary` into a readable line.
At go-live the real poster will likely hand the intent to the agent to compose
voice-appropriate copy; this structured render is the deterministic fallback and
what the dry-run rehearsal uses.

Posters are callables `(intent) -> bool` (True if posted), matching IntentConsumer.
"""
from __future__ import annotations


def _clean_value(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _looks_like_player_tag(value: str | None) -> bool:
    if not value:
        return False
    text = value.strip()
    return text.startswith("#") and text[1:].isalnum()


def _subject_label(intent, fallback: str = "A clanmate") -> str:
    s = intent.summary or {}
    for key in ("player_name", "member_name", "current_name", "name"):
        value = _clean_value(s.get(key))
        if value and not _looks_like_player_tag(value):
            return value
    subject = _clean_value(intent.subject_tag)
    if subject and not _looks_like_player_tag(subject):
        return subject
    return fallback


def _with_article(value: str | None, fallback: str) -> str:
    return _clean_value(value) or fallback


def _join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _member_names(summary: dict) -> list[str]:
    names: list[str] = []
    for member in summary.get("members") or []:
        if not isinstance(member, dict):
            continue
        name = _clean_value(member.get("name"))
        if name and not _looks_like_player_tag(name):
            names.append(name)
    return names


def render_intent(intent) -> str:
    s = intent.summary or {}
    subj = _subject_label(intent)
    t = intent.intent_type or ""
    if t.startswith("celebrate:"):
        dt = s.get("detection_type", t.split(":", 1)[-1])
        if dt == "best_trophies_peak":
            peak = _clean_value(s.get("peak"))
            return f"🏆 {subj} hit a new trophy best" + (f" of {peak}!" if peak else "!")
        if dt == "battle_hot_streak":
            streak = _clean_value(s.get("streak"))
            return f"🔥 {subj} is on a hot streak" + (f" — {streak} wins straight!" if streak else "!")
        if dt == "battle_trophy_push":
            delta = _clean_value(s.get("trophy_delta"))
            battles = _clean_value(s.get("battle_count"))
            if delta and battles:
                return f"📈 {subj} pushed +{delta} trophies over {battles} battles."
            return f"📈 {subj} is climbing."
        if dt == "career_wins_milestone":
            milestone = _clean_value(s.get("milestone"))
            return f"🏆 {subj} reached {milestone} career wins!" if milestone else f"🏆 {subj} reached a career wins milestone."
        if dt == "card_level_milestone":
            card = _with_article(s.get("card_name"), "a card")
            milestone = _clean_value(s.get("milestone"))
            return f"⭐ {subj} took {card} to level {milestone}." if milestone else f"⭐ {subj} leveled up {card}."
        if dt == "collection_level_milestone":
            milestone = _clean_value(s.get("milestone"))
            return f"📚 {subj} reached collection level {milestone}." if milestone else f"📚 {subj} reached a collection milestone."
        if dt == "new_card_unlocked":
            card = _with_article(s.get("card_name"), "a new card")
            rarity = _clean_value(s.get("rarity"))
            suffix = f" ({rarity})." if rarity else "."
            return f"🎉 {subj} unlocked {card}{suffix}"
        if dt == "new_champion_unlocked":
            card = _with_article(s.get("card_name"), "a Champion")
            return f"👑 {subj} unlocked Champion {card}!"
        if dt == "badge_earned":
            badge = _clean_value(s.get("badge_name"))
            return f"🎖️ {subj} earned the {badge} badge." if badge else f"🎖️ {subj} earned a new badge."
        if dt == "player_level_up":
            level = _clean_value(s.get("level"))
            return f"⬆️ {subj} reached King level {level}." if level else f"⬆️ {subj} reached a new King level."
        return f"{subj} hit a new clan milestone."
    if t.startswith("cohort:"):
        wave_type = _clean_value(s.get("wave_type") or s.get("detection_type"))
        names = _member_names(s)
        label = _join_names(names) if names else None
        member_count = _clean_value(s.get("member_count"))
        count = member_count or "multiple"
        if wave_type == "badge_earned":
            if label:
                return f"🎖️ {label} earned new badges today."
            return f"🎖️ {count} POAP KINGS members earned new badges today."
        if wave_type == "card_level_milestone":
            if label:
                return f"👑 {label} leveled cards today."
            return f"👑 {count} POAP KINGS members leveled cards today."
        if wave_type == "new_card_unlocked":
            if label:
                return f"✨ {label} unlocked new cards today."
            return f"✨ {count} POAP KINGS members unlocked new cards today."
        if label:
            return f"✨ {label} hit fresh milestones today."
        return f"✨ {count} POAP KINGS members hit fresh milestones today."
    if t.startswith("clan:"):
        dt = s.get("detection_type", t.split(":", 1)[-1])
        if dt == "member_joined":
            return f"Welcome to POAP KINGS, {_subject_label(intent, 'new member')}."
        if dt == "member_left":
            return "A member left POAP KINGS."
        if dt in {"member_promoted", "role_change"}:
            return f"👑 {_subject_label(intent)} earned a new clan role."
        return "POAP KINGS has a new clan update."
    if t.startswith("war:"):
        dt = s.get("detection_type", t.split(":", 1)[-1])
        if dt == "war_complete":
            rank = _clean_value(s.get("final_rank"))
            fame = _clean_value(s.get("our_fame") or s.get("our_active_score"))
            if rank and fame:
                return f"🏁 River Race complete: POAP KINGS finished rank {rank} with {fame} fame."
            return "🏁 River Race complete for POAP KINGS."
        return "⚔️ POAP KINGS has a fresh River Race update."
    if t.startswith("leadership:"):
        rec = s.get("recommendation_type", t.split(":", 1)[-1])
        reasons = ", ".join(s.get("reason_codes", []) or [])
        return f"Leadership review: {rec}" + (f" ({reasons})" if reasons else "")
    return "POAP KINGS has a new update."


class DryRunPoster:
    """Renders + records intents without posting. For the Stage-5 rehearsal."""

    def __init__(self):
        self.posts: list[tuple[str, str]] = []

    def __call__(self, intent) -> bool:
        self.posts.append((intent.scope, render_intent(intent)))
        return True


def make_discord_poster(send):
    """Wrap a Discord send function `send(scope, text) -> bool` as a poster.

    Wired at go-live to the real client; the renderer (or the agent) supplies copy.
    """
    def poster(intent) -> bool:
        return bool(send(intent.scope, render_intent(intent)))

    return poster
