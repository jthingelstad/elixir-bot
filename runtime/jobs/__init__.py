"""Recurring job executors for Elixir."""

# Re-export runtime_status so `runtime_jobs.runtime_status` still works
from runtime import status as runtime_status  # noqa: F401
from runtime.helpers import (  # noqa: F401
    _WEEKLY_RECAP_HEADER_RE,
    _channel_config_by_key,
    _format_weekly_recap_post,
    _strip_weekly_recap_header,
)
from runtime.jobs._battle_intel import (  # noqa: F401
    BATTLE_INTEL_BATCH,
    BATTLE_INTEL_PROSE_MINUTES,
    BATTLE_INTEL_STAGE_A_MINUTES,
    BATTLE_INTEL_STAGE_B_MINUTES,
    _battle_intel_prose,
    _battle_intel_stage_a,
    _battle_intel_stage_b,
)
from runtime.jobs._core import (  # noqa: F401
    WEEKLY_DISCORD_INVITE_RELAY_DAY,
    WEEKLY_DISCORD_INVITE_RELAY_HOUR,
    WEEKLY_MEMBER_REPORT_DAY,
    WEEKLY_MEMBER_REPORT_HOUR,
    WEEKLY_RECAP_DAY,
    WEEKLY_RECAP_HOUR,
    _ask_elixir_daily_insight,
    _build_ask_elixir_daily_insight_context,
    _query_or_default,
    _summarize_member_rows,
    _weekly_clan_recap,
    _weekly_discord_invite_relay,
    _weekly_member_report_cycle,
)
from runtime.jobs._intel import (  # noqa: F401
    _clan_wars_intel_report,
)
from runtime.jobs._maintenance import (  # noqa: F401
    API_SENTINEL_POLL_MINUTES,
    _api_sentinel_tick,
    _build_maintenance_report,
    _card_catalog_sync,
    _db_maintenance_cycle,
    _format_size,
)
from runtime.jobs._memory import (  # noqa: F401
    MEMORY_SYNTHESIS_DAY,
    MEMORY_SYNTHESIS_HOUR,
    MEMORY_SYNTHESIS_MEMORY_BODY_CHARS,
    MEMORY_SYNTHESIS_MEMORY_LIMIT,
    MEMORY_SYNTHESIS_POST_CHARS,
    MEMORY_SYNTHESIS_POSTS_PER_CHANNEL,
    MEMORY_SYNTHESIS_PRIOR_ARC_LIMIT,
    _apply_memory_synthesis_plan,
    _build_memory_synthesis_context,
    _memory_synthesis_cycle,
)

# _post_to_elixir and _load_live_clan_context are delegation wrappers in
# _signals.py that forward to runtime.app — excluded to avoid recursion.
from runtime.jobs._promotion import (  # noqa: F401
    _promotion_channel_posts,
    _promotion_content_cycle,
    _promotion_discord_required_text,
    _promotion_reddit_required_token,
    _unwrap_outer_bold,
    _validate_promote_content_or_raise,
)
from runtime.jobs._tournament import (  # noqa: F401
    _TOURNAMENT_JOB_ID,
    TOURNAMENT_BATTLE_LOG_SPACING_SECONDS,
    TOURNAMENT_POLL_MINUTES,
    _tournament_recap,
    _tournament_watch_tick,
    maybe_autowatch_tournament,
    start_tournament_watch,
    stop_tournament_watch,
)

# _build_weekly_clan_recap_context is a delegation wrapper in _core.py that
# forwards to runtime.app — re-exporting it here would create a recursive loop
# when tests access it via the top-level elixir module.
