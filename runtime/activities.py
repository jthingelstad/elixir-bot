from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeAttrRef:
    name: str
    default: Any = None


@dataclass(frozen=True)
class ActivityDefinition:
    activity_key: str
    owner_subagent: str
    purpose: str
    job_id: str
    job_function: str
    schedule_kind: str
    schedule_config: dict[str, Any]
    delivery_targets: tuple[str, ...]
    manual_trigger_allowed: bool = True
    enabled_by_default: bool = True
    active_window: dict[str, Any] | None = None
    legacy_commands: tuple[str, ...] = ()


def _attr(name: str, default: Any = None) -> RuntimeAttrRef:
    return RuntimeAttrRef(name=name, default=default)


_ACTIVITIES: tuple[ActivityDefinition, ...] = (
    ActivityDefinition(
        activity_key="clan-awareness",
        owner_subagent="clan-events",
        purpose="Process non-war clan signals, leader-facing notes, and routed clan-event outcomes.",
        job_id="clan-awareness",
        job_function="_clan_awareness_tick",
        schedule_kind="interval",
        schedule_config={
            "minutes": _attr("HEARTBEAT_INTERVAL_MINUTES", 30),
            "jitter": _attr("HEARTBEAT_JITTER_SECONDS", 900),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Discord routed outcomes: #clan-events, #leader-lounge",
        ),
        legacy_commands=("heartbeat",),
    ),
    ActivityDefinition(
        activity_key="war-poll",
        owner_subagent="river-race",
        purpose="Poll live war state and persist the hourly River Race snapshot pipeline.",
        job_id="war-poll",
        job_function="_war_poll_tick",
        schedule_kind="cron",
        schedule_config={
            "minute": _attr("WAR_POLL_MINUTE", 0),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Storage: live war snapshots and river race log",
        ),
        manual_trigger_allowed=False,
    ),
    ActivityDefinition(
        activity_key="war-awareness",
        owner_subagent="river-race",
        purpose="Process war-only signals and coordinate River Race messaging.",
        job_id="war-awareness",
        job_function="_war_awareness_tick",
        schedule_kind="cron",
        schedule_config={
            "minute": _attr("WAR_AWARENESS_MINUTE", 5),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Discord routed outcomes: #river-race, optional #leader-lounge",
        ),
    ),
    ActivityDefinition(
        activity_key="player-progression",
        owner_subagent="player-progress",
        purpose="Refresh player intelligence and emit progression milestones.",
        job_id="player-progression",
        job_function="_player_intel_refresh",
        schedule_kind="interval",
        schedule_config={
            "minutes": _attr("PLAYER_INTEL_REFRESH_MINUTES", 30),
            "jitter": _attr("PLAYER_INTEL_REFRESH_JITTER_SECONDS", 900),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Discord: #player-progress",
        ),
        legacy_commands=("player-intel",),
    ),
    ActivityDefinition(
        activity_key="daily-clan-insight",
        owner_subagent="ask-elixir",
        purpose="Share one playful, data-driven hidden fact in the dedicated Elixir conversation channel.",
        job_id="daily-clan-insight",
        job_function="_ask_elixir_daily_insight",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("ASK_ELIXIR_DAILY_INSIGHT_HOUR", 12),
            "minute": _attr("ASK_ELIXIR_DAILY_INSIGHT_MINUTE", 0),
            "jitter": _attr("ASK_ELIXIR_DAILY_INSIGHT_JITTER_SECONDS", 1800),
        },
        delivery_targets=(
            "Discord: #ask-elixir",
        ),
    ),
    ActivityDefinition(
        activity_key="leadership-review",
        owner_subagent="leader-lounge",
        purpose="Post the weekly leadership review for clan operations.",
        job_id="leadership-review",
        job_function="_clanops_weekly_review",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("CLANOPS_WEEKLY_REVIEW_DAY", "fri"),
            "hour": _attr("CLANOPS_WEEKLY_REVIEW_HOUR", 19),
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #leader-lounge",
        ),
        legacy_commands=("clanops-review",),
    ),
    ActivityDefinition(
        activity_key="memory-synthesis",
        owner_subagent="leader-lounge",
        purpose="Weekly pass that writes arc memories, retires stale entries, and flags contradictions against live state.",
        job_id="memory-synthesis",
        job_function="_memory_synthesis_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("MEMORY_SYNTHESIS_DAY", "sun"),
            "hour": _attr("MEMORY_SYNTHESIS_HOUR", 22),
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #leader-lounge",
        ),
    ),
    ActivityDefinition(
        activity_key="weekly-recap",
        owner_subagent="announcements",
        purpose="Publish the public weekly clan recap and members-page payload.",
        job_id="weekly-recap",
        job_function="_weekly_clan_recap",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("WEEKLY_RECAP_DAY", "mon"),
            "hour": _attr("WEEKLY_RECAP_HOUR", 9),
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #announcements",
            "POAP KINGS: weekly members payload",
        ),
    ),
    ActivityDefinition(
        activity_key="site-content",
        owner_subagent="announcements",
        purpose="Refresh and publish daily POAP KINGS site content.",
        job_id="site-content",
        job_function="_site_content_cycle",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("SITE_CONTENT_HOUR", 18),
            "minute": 0,
        },
        delivery_targets=(
            "POAP KINGS: home payload",
            "POAP KINGS: clan payload",
            "POAP KINGS: roster payload",
        ),
        legacy_commands=("poap-kings-site-sync",),
    ),
    ActivityDefinition(
        activity_key="promotion-content",
        owner_subagent="promote-the-clan",
        purpose="Generate reusable recruiting content for members and the website.",
        job_id="promotion-content",
        job_function="_promotion_content_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("PROMOTION_CONTENT_DAY", "fri"),
            "hour": _attr("PROMOTION_CONTENT_HOUR", 9),
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #promote-the-clan",
            "POAP KINGS: promotion payloads",
        ),
        legacy_commands=("promotion",),
    ),
    ActivityDefinition(
        activity_key="daily-quiz",
        owner_subagent="ask-elixir",
        purpose="Post the daily Elixir University quiz question in #card-quiz.",
        job_id="daily-quiz",
        job_function="_daily_quiz_post",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("DAILY_QUIZ_HOUR", 10),
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #card-quiz",
        ),
    ),
    ActivityDefinition(
        activity_key="card-catalog-sync",
        owner_subagent="leader-lounge",
        purpose="Sync the Clash Royale card catalog from the API.",
        job_id="card-catalog-sync",
        job_function="_card_catalog_sync",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("CARD_CATALOG_SYNC_HOUR", 4),
            "minute": 0,
        },
        delivery_targets=(
            "Storage: card_catalog table",
        ),
    ),
    ActivityDefinition(
        activity_key="db-maintenance",
        owner_subagent="leader-lounge",
        purpose="Purge expired data, VACUUM the database, and report space reclaimed.",
        job_id="db-maintenance",
        job_function="_db_maintenance_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("DB_MAINTENANCE_DAY", "sun"),
            "hour": _attr("DB_MAINTENANCE_HOUR", 2),
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #leader-lounge",
        ),
    ),
    ActivityDefinition(
        activity_key="clan-wars-intel",
        owner_subagent="river-race",
        purpose="Generate a detailed intel report on competing clans for the war season.",
        job_id="clan-wars-intel",
        job_function="_clan_wars_intel_report",
        schedule_kind="cron",
        schedule_config={
            "month": "*",
            "day": 1,
            "hour": 12,
            "minute": 0,
        },
        delivery_targets=(
            "Discord: #river-race",
        ),
        manual_trigger_allowed=True,
        enabled_by_default=False,
        legacy_commands=("intel-report",),
    ),
)


def list_registered_activities() -> list[ActivityDefinition]:
    return list(_ACTIVITIES)


def _activity_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for activity in _ACTIVITIES:
        aliases[activity.activity_key] = activity.activity_key
        aliases[activity.job_id] = activity.activity_key
        aliases[activity.job_function] = activity.activity_key
        for alias in activity.legacy_commands:
            aliases[alias] = activity.activity_key
    return aliases


def normalize_activity_key(value: str | None) -> str | None:
    if value is None:
        return None
    return _activity_aliases().get((value or "").strip().lower())


def get_activity(activity_key: str | None) -> ActivityDefinition | None:
    normalized = normalize_activity_key(activity_key)
    if normalized is None:
        return None
    for activity in _ACTIVITIES:
        if activity.activity_key == normalized:
            return activity
    return None


def _resolve_runtime_value(value: Any, runtime_module: Any) -> Any:
    if isinstance(value, RuntimeAttrRef):
        return getattr(runtime_module, value.name, value.default)
    return value


def _resolve_mapping(values: dict[str, Any] | None, runtime_module: Any) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key, value in (values or {}).items():
        resolved[key] = _resolve_runtime_value(value, runtime_module)
    return resolved


def resolve_activity(activity_key: str, runtime_module: Any) -> dict[str, Any]:
    activity = get_activity(activity_key)
    if activity is None:
        raise KeyError(f"unknown activity: {activity_key}")
    return {
        "definition": activity,
        "activity_key": activity.activity_key,
        "owner_subagent": activity.owner_subagent,
        "purpose": activity.purpose,
        "job_id": activity.job_id,
        "job_function": activity.job_function,
        "job_callable": getattr(runtime_module, activity.job_function),
        "schedule_kind": activity.schedule_kind,
        "schedule_config": _resolve_mapping(activity.schedule_config, runtime_module),
        "active_window": _resolve_mapping(activity.active_window, runtime_module) if activity.active_window else None,
        "delivery_targets": list(activity.delivery_targets),
        "manual_trigger_allowed": activity.manual_trigger_allowed,
        "enabled_by_default": activity.enabled_by_default,
    }


def _format_day(value: str) -> str:
    return (value or "").strip().title()


def _format_hour(value: int) -> str:
    return f"{int(value):02d}:00 CT"


def _format_human_jitter(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds % 60 == 0:
        minutes = seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        return f" with up to {minutes} {unit} jitter."
    return f" with up to {seconds}s jitter."


def _format_schedule_description(resolved: dict[str, Any]) -> str:
    schedule_kind = resolved["schedule_kind"]
    schedule_config = resolved["schedule_config"]
    active_window = resolved.get("active_window") or {}
    if schedule_kind == "interval":
        minutes = schedule_config.get("minutes")
        parts = [f"Every {minutes} minutes."]
        jitter = schedule_config.get("jitter")
        if jitter:
            parts[0] = f"Every {minutes} minutes with up to {int(jitter)}s jitter."
        if active_window:
            parts.append(
                "Active hours "
                f"{active_window.get('start_hour')}:00-{active_window.get('end_hour')}:00 "
                f"{active_window.get('timezone', 'local')}."
            )
        return " ".join(part for part in parts if part)

    day_of_week = schedule_config.get("day_of_week")
    minute = int(schedule_config.get("minute", 0))
    if day_of_week:
        hour = schedule_config.get("hour", 0)
        return f"Every {_format_day(day_of_week)} at {hour:02d}:{minute:02d} CT.{_format_human_jitter(schedule_config.get('jitter'))}"
    if "hour" in schedule_config:
        hour = schedule_config.get("hour", 0)
        return f"Daily at {hour:02d}:{minute:02d} CT.{_format_human_jitter(schedule_config.get('jitter'))}"
    return f"Every hour at :{minute:02d} CT.{_format_human_jitter(schedule_config.get('jitter'))}"


def schedule_specs_from_registry(runtime_module: Any) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for activity in _ACTIVITIES:
        resolved = resolve_activity(activity.activity_key, runtime_module)
        specs.append(
            {
                "activity_key": activity.activity_key,
                "owner_subagent": activity.owner_subagent,
                "purpose": activity.purpose,
                "job_id": activity.job_id,
                "job_function": activity.job_function,
                "schedule_kind": activity.schedule_kind,
                "schedule_config": resolved["schedule_config"],
                "active_window": resolved["active_window"],
                "schedule": _format_schedule_description(resolved),
                "delivery_targets": list(activity.delivery_targets),
                "manual_trigger_allowed": activity.manual_trigger_allowed,
            }
        )
    return specs


def manual_activity_commands() -> list[str]:
    return [activity.activity_key for activity in _ACTIVITIES if activity.manual_trigger_allowed]


def manual_activity_choices() -> list[tuple[str, str]]:
    labels: list[tuple[str, str]] = []
    for activity in _ACTIVITIES:
        if not activity.manual_trigger_allowed:
            continue
        legacy = f" [{activity.legacy_commands[0]}]" if activity.legacy_commands else ""
        labels.append((f"{activity.activity_key}{legacy}", activity.activity_key))
    return labels


def register_scheduled_activities(*, scheduler: Any, runtime_module: Any, create_task: Any) -> list[dict[str, Any]]:
    registered: list[dict[str, Any]] = []
    for activity in _ACTIVITIES:
        if not activity.enabled_by_default:
            continue
        resolved = resolve_activity(activity.activity_key, runtime_module)
        scheduler.add_job(
            create_task(resolved["job_callable"]),
            resolved["schedule_kind"],
            id=resolved["job_id"],
            name=resolved["activity_key"],
            **resolved["schedule_config"],
        )
        registered.append(resolved)
    return registered


def format_scheduler_startup_summary(runtime_module: Any) -> str:
    parts = []
    for spec in schedule_specs_from_registry(runtime_module):
        parts.append(f"{spec['activity_key']} — {spec['schedule']}")
    return ", ".join(parts)


__all__ = [
    "ActivityDefinition",
    "get_activity",
    "list_registered_activities",
    "manual_activity_choices",
    "manual_activity_commands",
    "normalize_activity_key",
    "register_scheduled_activities",
    "resolve_activity",
    "schedule_specs_from_registry",
    "format_scheduler_startup_summary",
]
