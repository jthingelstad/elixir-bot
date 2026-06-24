"""Discord rendering + posters for communication intents.

`render_intent` turns an intent's presentation-free `summary` into a readable line.
At go-live the real poster will likely hand the intent to the agent to compose
voice-appropriate copy; this structured render is the deterministic fallback and
what the dry-run rehearsal uses.

Posters are callables `(intent) -> bool` (True if posted), matching IntentConsumer.
"""
from __future__ import annotations


def render_intent(intent) -> str:
    s = intent.summary or {}
    subj = intent.subject_tag or ""
    t = intent.intent_type or ""
    if t.startswith("celebrate:"):
        dt = s.get("detection_type", t.split(":", 1)[-1])
        templates = {
            "best_trophies_peak": f"🏆 {subj} hit a new best of {s.get('peak')} trophies!",
            "battle_hot_streak": f"🔥 {subj} is on a {s.get('streak', '')}-win streak!",
            "battle_trophy_push": f"📈 {subj} pushed +{s.get('trophy_delta')} over {s.get('battle_count')} battles.",
            "career_wins_milestone": f"🏆 {subj} reached {s.get('milestone')} career wins!",
            "card_level_milestone": f"⭐ {subj} took {s.get('card_name')} to level {s.get('milestone')}.",
            "collection_level_milestone": f"📚 {subj} reached collection level {s.get('milestone')}.",
            "new_card_unlocked": f"🎉 {subj} unlocked {s.get('card_name')} ({s.get('rarity')}).",
            "new_champion_unlocked": f"👑 {subj} unlocked Champion {s.get('card_name')}!",
            "badge_earned": f"🎖️ {subj} earned the {s.get('badge_name')} badge.",
            "player_level_up": f"⬆️ {subj} reached King level {s.get('level')}.",
        }
        return templates.get(dt, f"{subj}: {dt} {s}")
    if t.startswith("leadership:"):
        rec = s.get("recommendation_type", t.split(":", 1)[-1])
        reasons = ", ".join(s.get("reason_codes", []) or [])
        return f"[leadership] {rec} candidate: {subj} ({reasons})"
    return f"[{intent.scope}] {t} {subj}: {s}"


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
