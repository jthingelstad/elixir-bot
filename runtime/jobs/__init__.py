"""Recurring job executors for Elixir."""

from runtime.jobs._core import (  # noqa: F401
    _player_intel_refresh_minutes, PLAYER_INTEL_REFRESH_MINUTES,
    PLAYER_INTEL_REFRESH_HOURS, WAR_POLL_MINUTE, WAR_AWARENESS_MINUTE,
    PLAYER_INTEL_BATCH_SIZE, PLAYER_INTEL_STALE_HOURS,
    PLAYER_INTEL_REQUEST_SPACING_SECONDS, PLAYER_INTEL_REFRESH_JITTER_SECONDS,
    CLANOPS_WEEKLY_REVIEW_DAY, CLANOPS_WEEKLY_REVIEW_HOUR,
    WEEKLY_RECAP_DAY, WEEKLY_RECAP_HOUR,
    _query_or_default, _summarize_member_rows,
    _build_ask_elixir_daily_insight_context, _ask_elixir_daily_insight,
    _clan_awareness_tick, _war_poll_tick, _war_awareness_tick,
    _player_intel_refresh, _clanops_weekly_review, _weekly_clan_recap,
)
# _build_weekly_clanops_review and _build_weekly_clan_recap_context are
# delegation wrappers in _core.py that forward to runtime.app — re-exporting
# them here would create a recursive loop when tests access them via the
# top-level elixir module.
from runtime.jobs._signals import (  # noqa: F401
    _WEEKLY_RECAP_HEADER_RE,
    _channel_config_by_key, _signal_group_needs_recap_memory,
    _build_outcome_context, _mark_signal_group_completed, _post_signal_memory,
    _deliver_signal_outcome, _deliver_signal_group,
    _strip_weekly_recap_header, _format_weekly_recap_post,
    _observation_signal_batches, _progression_signal_batches,
    _system_signal_updates, _store_recap_memories_for_signal_batch,
    _build_system_signal_context, _preauthored_system_signal_result,
    _post_system_signal_updates, _publish_pending_system_signal_updates,
    _mark_delivered_signals, _persist_signal_detector_cursors,
)
# _post_to_elixir and _load_live_clan_context are delegation wrappers in
# _signals.py that forward to runtime.app — excluded to avoid recursion.
from runtime.jobs._site import (  # noqa: F401
    SITE_DATA_HOUR, SITE_CONTENT_HOUR,
    _promotion_discord_required_text, _promotion_reddit_required_token,
    _promotion_channel_posts, _unwrap_outer_bold,
    _validate_promote_content_or_raise, _write_site_content_or_raise,
    _commit_site_content_or_raise, _publish_poap_kings_site_or_raise,
    _normalize_poap_kings_publish_result, _poapkings_publish_context,
    _poapkings_publish_fallback, _notify_poapkings_publish,
    _promotion_content_cycle, _site_data_refresh, _site_content_cycle,
)
from runtime.jobs._tournament import (  # noqa: F401
    TOURNAMENT_POLL_MINUTES, TOURNAMENT_BATTLE_LOG_SPACING_SECONDS,
    _TOURNAMENT_JOB_ID, _tournament_watch_tick, _tournament_recap,
    start_tournament_watch, stop_tournament_watch,
)
from runtime.jobs._maintenance import (  # noqa: F401
    _format_size, _build_maintenance_report, _daily_quiz_post,
    _card_catalog_sync, _db_maintenance_cycle,
)

# Re-export runtime_status so `runtime_jobs.runtime_status` still works
from runtime import status as runtime_status  # noqa: F401
