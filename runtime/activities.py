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
    owner_lane: str
    purpose: str
    job_id: str
    job_function: str
    schedule_kind: str
    schedule_config: dict[str, Any]
    delivery_targets: tuple[str, ...]
    activity_role: str = "communicator"
    manual_trigger_allowed: bool = True
    enabled_by_default: bool = True
    active_window: dict[str, Any] | None = None


def _attr(name: str, default: Any = None) -> RuntimeAttrRef:
    return RuntimeAttrRef(name=name, default=default)


_ACTIVITIES: tuple[ActivityDefinition, ...] = (
    ActivityDefinition(
        activity_key="engine-tick",
        owner_lane="player-highlights",
        purpose="The v5.1 production data tick (runtime.md §2): adaptive-budget "
        "poll → battle mirror → emit → project → manage. It refreshes the "
        "streams/projections the awareness loop reads; the retired deterministic "
        "recognizer/delivery path is not run. Awards fire on season_closed.",
        job_id="engine-tick",
        job_function="_engine_tick",
        schedule_kind="interval",
        schedule_config={
            "minutes": _attr("ENGINE_TICK_MINUTES", 10),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Storage: event streams, projections, management state; leadership "
            "action cards only when management transitions require them",
        ),
        activity_role="observer",
    ),
    ActivityDefinition(
        activity_key="weekly-leadership-review",
        owner_lane="arena-relay",
        purpose="Q1's weekly batch half: roll the management week (hysteresis "
        "counters advance ONLY here), surface promote/demote candidacies as "
        "leader actions, post one review summary. Monday 7:00 AM America/Chicago "
        "(ratified 2026-07-03).",
        job_id="weekly-leadership-review",
        job_function="_weekly_leadership_review",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("WEEKLY_REVIEW_DAY", "mon"),
            "hour": _attr("WEEKLY_REVIEW_HOUR", 7),
            "minute": _attr("WEEKLY_REVIEW_MINUTE", 0),
            "timezone": "America/Chicago",
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=("Discord: #actions weekly review + recommendation cards",),
        activity_role="observer+communicator",
    ),
    ActivityDefinition(
        activity_key="war-attendance-snapshot",
        owner_lane="arena-relay",  # #river-race retired; data-only job (no posts)
        purpose="Finalize war_attendance_days for the just-closed war day "
        "(evaluators read finalized days only — runtime.md §3). CR war days "
        "roll at ~09:37–10:00 UTC; this runs just before the boundary.",
        job_id="war-attendance-snapshot",
        job_function="_war_attendance_snapshot",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("WAR_ATTENDANCE_HOUR", 4),
            "minute": _attr("WAR_ATTENDANCE_MINUTE", 15),
            "timezone": "America/Chicago",
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=("Storage: finalized war_attendance_days rows",),
        activity_role="observer",
        manual_trigger_allowed=False,
    ),
    ActivityDefinition(
        activity_key="action-outcome-refresh",
        owner_lane="arena-relay",
        purpose="Daily leader-action hygiene carried from leadership-action-scan: "
        "refresh due action outcomes and re-queue feedback synthesis. The scan/"
        "creation role is retired (the engine tick owns the Q1 reactive path).",
        job_id="action-outcome-refresh",
        job_function="_action_outcome_refresh",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("ACTION_OUTCOME_REFRESH_HOUR", 9),
            "minute": _attr("ACTION_OUTCOME_REFRESH_MINUTE", 30),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=("Storage: leader-action outcome/feedback rows",),
        activity_role="observer",
    ),
    ActivityDefinition(
        activity_key="daily-clan-insight",
        owner_lane="ask-elixir",
        purpose="Capability-discovery spotlight: one thing Elixir can do, one real data nugget, copy-pasteable next questions.",
        job_id="daily-clan-insight",
        job_function="_ask_elixir_daily_insight",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("ASK_ELIXIR_DAILY_INSIGHT_HOUR", 12),
            "minute": _attr("ASK_ELIXIR_DAILY_INSIGHT_MINUTE", 0),
        },
        delivery_targets=("Discord: #ask-elixir",),
        activity_role="communicator",
    ),
    ActivityDefinition(
        activity_key="weekly-discord-invite-relay",
        owner_lane="arena-relay",
        purpose="Evergreen housekeeping nudges (Discord, POAP KINGS FAQ, website): "
        "rotate the evergreen_nudges inventory and, ONLY during a quiet period and "
        "within a strict rate cap, offer ONE as an in-game-relay leader-action card "
        "in #actions. Runs daily; self-gates so it emits rarely.",
        job_id="weekly-discord-invite-relay",
        job_function="_weekly_discord_invite_relay",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("EVERGREEN_NUDGE_HOUR", 13),
            "minute": 0,
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=("Discord: #actions in-game-relay nudge card (quiet periods only)",),
        activity_role="communicator",
    ),
    ActivityDefinition(
        activity_key="member-outreach-propose",
        owner_lane="arena-relay",
        purpose="DM-outreach (Phase 1): pick current members missing a verified "
        "email and offer up to a few leader-gated 'Profile Outreach' cards in "
        "#actions. FULLY DORMANT unless ELIXIR_DM_OUTREACH=1; a leader approves "
        "each card before any DM is delivered.",
        job_id="member-outreach-propose",
        job_function="_member_outreach_propose",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("MEMBER_OUTREACH_HOUR", 14),
            "minute": 30,
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=("Discord: #actions Profile Outreach card (leader-gated + flag-gated)",),
        activity_role="communicator",
    ),
    ActivityDefinition(
        activity_key="memory-synthesis",
        owner_lane="leader-lounge",
        purpose="Weekly pass that writes arc memories, retires stale entries, and flags contradictions against live state.",
        job_id="memory-synthesis",
        job_function="_memory_synthesis_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("MEMORY_SYNTHESIS_DAY", "sun"),
            "hour": _attr("MEMORY_SYNTHESIS_HOUR", 22),
            "minute": 0,
        },
        delivery_targets=("Discord: #leaders",),
        activity_role="observer+communicator",
    ),
    ActivityDefinition(
        activity_key="weekly-recap",
        owner_lane="announcements",
        purpose="Publish the public weekly clan recap and members-page payload.",
        job_id="weekly-recap",
        job_function="_weekly_clan_recap",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("WEEKLY_RECAP_DAY", "mon"),
            "hour": _attr("WEEKLY_RECAP_HOUR", 9),
            "minute": 0,
        },
        delivery_targets=("Discord: #announcements",),
        activity_role="communicator",
    ),
    ActivityDefinition(
        activity_key="weekly-member-report",
        owner_lane="announcements",
        purpose="Arena Dispatch: email each member with a verified address a "
        "personalized weekly Clash Royale report on their own play, badges, "
        "cards, and battles. Runs an hour after the public clan recap.",
        job_id="weekly-member-report",
        job_function="_weekly_member_report_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("WEEKLY_MEMBER_REPORT_DAY", "mon"),
            "hour": _attr("WEEKLY_MEMBER_REPORT_HOUR", 10),
            "minute": 0,
        },
        delivery_targets=("Email: each member (To:, individual)",),
        activity_role="communicator",
    ),
    ActivityDefinition(
        activity_key="weekly-elder-standing",
        owner_lane="announcements",
        purpose="Publish the transparent weekly Elder Standing to #announcements — "
        "who's holding Elder, who's rising toward it, and who's on the stepping-down "
        "bubble, each with their participation. Runs Tuesday after the promo/demo "
        "review has rolled the week.",
        job_id="weekly-elder-standing",
        job_function="_weekly_elder_standing",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("WEEKLY_ELDER_STANDING_DAY", "tue"),
            "hour": _attr("WEEKLY_ELDER_STANDING_HOUR", 12),
            "minute": 0,
        },
        delivery_targets=("Discord: #announcements",),
        activity_role="communicator",
    ),
    # site-content (POAP KINGS website publishing) was removed entirely 2026-06-21
    # — the website has its own standalone update script now.
    ActivityDefinition(
        activity_key="promotion-content",
        owner_lane="promote-the-clan",
        purpose="Generate reusable recruiting content for the #recruiting channel.",
        job_id="promotion-content",
        job_function="_promotion_content_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("PROMOTION_CONTENT_DAY", "fri"),
            "hour": _attr("PROMOTION_CONTENT_HOUR", 9),
            "minute": 0,
        },
        delivery_targets=("Discord: #recruiting",),
        activity_role="communicator",
    ),
    ActivityDefinition(
        activity_key="card-catalog-sync",
        owner_lane="leader-lounge",
        purpose="Sync the Clash Royale card catalog from the API.",
        job_id="card-catalog-sync",
        job_function="_card_catalog_sync",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("CARD_CATALOG_SYNC_HOUR", 4),
            "minute": 0,
        },
        delivery_targets=("Storage: card_catalog table",),
        activity_role="observer",
    ),
    ActivityDefinition(
        activity_key="api-sentinel",
        owner_lane="leader-lounge",
        purpose="Track first-seen Clash Royale API schema paths and current game-mode events as the game evolves.",
        job_id="api-sentinel",
        job_function="_api_sentinel_tick",
        schedule_kind="interval",
        schedule_config={
            "minutes": _attr("API_SENTINEL_POLL_MINUTES", 240),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Storage: api_sentinel_observations",
            "Discord: #leaders on first-seen CR API schema or event drift",
        ),
        activity_role="observer+communicator",
    ),
    ActivityDefinition(
        activity_key="db-backup",
        owner_lane="elixir-log",
        purpose="Daily compressed snapshot of the operational + memory databases "
        "to iCloud Drive (offsite via sync).",
        job_id="db-backup",
        job_function="_db_backup",
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("DB_BACKUP_HOUR", 3),
            "minute": 37,
        },
        delivery_targets=("Backup: timestamped iCloud database snapshot",),
        activity_role="observer",
    ),
    # player-pulse retired 2026-07-10: its #battle-feed posts rode the engine
    # delivery pipeline (now off) and battle-mode momentum is brain-owned in
    # #elixir. Removing the ActivityDefinition unschedules the job.
    ActivityDefinition(
        activity_key="engine-health",
        owner_lane="elixir-log",
        purpose="Daily read-only engine audit (tick errors, stuck intents, "
        "ledger duplicates, poll starvation, memory-write recency, db growth); "
        "posts to #elixir-log only when something is off.",
        job_id="engine-health",
        job_function="_engine_health",
        schedule_kind="cron",
        schedule_config={
            "hour": 8,
            "minute": 23,
        },
        delivery_targets=("Discord: #elixir-log on failed checks only",),
        activity_role="observer",
    ),
    # editorial-sweep + editorial-review retired 2026-07-10 with the Editor: the
    # brain composes with depth natively, so there's no template gate to tune and
    # no rubric to feed. Removing the ActivityDefinitions unschedules both jobs.
    ActivityDefinition(
        activity_key="db-maintenance",
        owner_lane="elixir-log",
        purpose="Purge expired data, VACUUM the database, and report space reclaimed.",
        job_id="db-maintenance",
        job_function="_db_maintenance_cycle",
        schedule_kind="cron",
        schedule_config={
            "day_of_week": _attr("DB_MAINTENANCE_DAY", "sun"),
            "hour": _attr("DB_MAINTENANCE_HOUR", 2),
            "minute": 0,
        },
        delivery_targets=("Discord webhook: #elixir-log",),
        activity_role="observer+communicator",
    ),
    ActivityDefinition(
        activity_key="clan-wars-intel",
        owner_lane="elixir",
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
        delivery_targets=("Discord: #elixir",),
        activity_role="communicator",
        manual_trigger_allowed=True,
        enabled_by_default=False,
    ),
    ActivityDefinition(
        activity_key="awareness-loop",
        owner_lane="elixir-log",
        purpose="The awareness loop (the central deliberative heartbeat): build "
        "the read, run the brain with its full read + write tool surface, persist "
        "the train of thought, deliver its post plan to the member-facing "
        "channels, and write a diagnostic to #elixir-log. The brain is the clan's "
        "sole proactive poster.",
        job_id="awareness-loop",
        job_function="_awareness_loop",
        # Wall-clock cron, not an interval: pinned to :05 past the scheduled hours
        # so the cadence is deterministic across restarts (an interval trigger
        # re-phases to "startup + N" every restart). :05 lands just after the top-
        # of-hour engine tick, so the brain reads freshly-refreshed state and dodges
        # the :00 cron crowd. AWARENESS_LOOP_HOURS ("*/3" = every 3h, widened from
        # hourly 2026-07-23 for cost) and AWARENESS_LOOP_MINUTE are the tunables.
        schedule_kind="cron",
        schedule_config={
            "hour": _attr("AWARENESS_LOOP_HOURS", "*/3"),
            "minute": _attr("AWARENESS_LOOP_MINUTE", 5),
            "max_instances": 1,
            "coalesce": True,
        },
        delivery_targets=(
            "Discord: member-facing lanes selected by the validated awareness plan",
            "Discord webhook: #elixir-log diagnostics",
        ),
        activity_role="observer+communicator",
        manual_trigger_allowed=True,
        enabled_by_default=True,
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
        "activity_role": activity.activity_role,
        "owner_lane": activity.owner_lane,
        "purpose": activity.purpose,
        "job_id": activity.job_id,
        "job_function": activity.job_function,
        "job_callable": getattr(runtime_module, activity.job_function),
        "schedule_kind": activity.schedule_kind,
        "schedule_config": _resolve_mapping(activity.schedule_config, runtime_module),
        "active_window": _resolve_mapping(activity.active_window, runtime_module)
        if activity.active_window
        else None,
        "delivery_targets": list(activity.delivery_targets),
        "manual_trigger_allowed": activity.manual_trigger_allowed,
        "enabled_by_default": activity.enabled_by_default,
    }


def _format_day(value: str) -> str:
    return (value or "").strip().title()


def _format_hour(value: int) -> str:
    return f"{int(value):02d}:00 CT"


def _format_schedule_description(resolved: dict[str, Any]) -> str:
    schedule_kind = resolved["schedule_kind"]
    schedule_config = resolved["schedule_config"]
    active_window = resolved.get("active_window") or {}
    if schedule_kind == "interval":
        minutes = schedule_config.get("minutes")
        parts = [f"Every {minutes} minutes."]
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
        return f"Every {_format_day(day_of_week)} at {hour:02d}:{minute:02d} CT."
    if "hour" in schedule_config:
        hour = schedule_config.get("hour", 0)
        # hour may be a cron expression (e.g. "*/3" every 3h, "0,6,12,18" a list),
        # not just a fixed int — describe those without int-formatting them.
        if isinstance(hour, str) and hour.startswith("*/"):
            return f"Every {hour[2:]} hours at :{minute:02d} CT."
        if isinstance(hour, str) and not hour.isdigit():
            return f"At hours {hour}, :{minute:02d} CT."
        return f"Daily at {int(hour):02d}:{minute:02d} CT."
    return f"Every hour at :{minute:02d} CT."


def schedule_specs_from_registry(runtime_module: Any) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for activity in _ACTIVITIES:
        resolved = resolve_activity(activity.activity_key, runtime_module)
        specs.append(
            {
                "activity_key": activity.activity_key,
                "activity_role": activity.activity_role,
                "owner_lane": activity.owner_lane,
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
        labels.append((activity.activity_key, activity.activity_key))
    return labels


def register_scheduled_activities(
    *, scheduler: Any, runtime_module: Any, create_task: Any
) -> list[dict[str, Any]]:
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
