"""Operator-visible logging for leader action decisions."""

from __future__ import annotations

from runtime import elixir_log

_MAX_FIELD_CHARS = 220


def _clip(value: object, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip(" .,;:") + "..."


def _target_line(name: str | None, tag: str | None) -> str | None:
    clean_name = _clip(name, 80)
    clean_tag = _clip(tag, 40)
    if clean_name and clean_tag:
        return f"Target: {clean_name} (`{clean_tag}`)"
    if clean_name:
        return f"Target: {clean_name}"
    if clean_tag:
        return f"Target: `{clean_tag}`"
    return None


async def post_leader_action_skip(
    *,
    source: str,
    action_type: str | None = None,
    reason: str | None = None,
    target_player_name: str | None = None,
    target_player_tag: str | None = None,
    objective: str | None = None,
    rationale: str | None = None,
    signal_types: set[str] | list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Post a concise #elixir-log record when Elixir decides not to show a card."""
    if not elixir_log.enabled():
        return False
    lines = [
        "🧭 Leader action not recommended",
        f"Source: `{_clip(source, 80) or 'unknown'}`",
    ]
    if action_type:
        lines.append(f"Type: `{_clip(action_type, 80)}`")
    target = _target_line(target_player_name, target_player_tag)
    if target:
        lines.append(target)
    if objective:
        lines.append(f"Objective: `{_clip(objective, 80)}`")
    if reason:
        lines.append(f"Reason: `{_clip(reason, 140)}`")
    if signal_types:
        clean_types = sorted({_clip(item, 60) for item in signal_types if _clip(item, 60)})
        if clean_types:
            lines.append(f"Signals: {', '.join(f'`{item}`' for item in clean_types[:6])}")
    if rationale:
        lines.append(f"Evidence: {_clip(rationale)}")
    return await elixir_log.post_event_async("\n".join(lines))


__all__ = ["post_leader_action_skip"]
