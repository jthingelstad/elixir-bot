import elixir  # noqa: F401
import runtime.jobs._signals as signal_facade


def test_signal_job_facade_exports_expected_callables():
    expected = {
        "_build_outcome_context",
        "_mark_signal_group_completed",
        "_post_signal_memory",
        "_deliver_signal_outcome",
        "_deliver_signal_group",
        "_deliver_awareness_post",
        "_deliver_awareness_post_plan",
        "_deliver_signal_group_via_awareness",
        "_system_signal_updates",
        "_store_recap_memories_for_signal_batch",
        "_build_system_signal_context",
        "_preauthored_system_signal_result",
        "_post_system_signal_updates",
        "_publish_pending_system_signal_updates",
        "_mark_delivered_signals",
        "_persist_signal_detector_cursors",
    }
    for name in expected:
        assert name in signal_facade.__all__
        assert callable(getattr(signal_facade, name))
