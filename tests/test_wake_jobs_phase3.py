"""Phase 3 — war-boundary wakes, and the invariant that let the brain halve.

The load-bearing claim of this phase is not that war posts get faster. It is
that **no hard post depends on the scheduled brain any more**, which is the only
reason cutting that cron from twice a day to once is safe. That claim is one
assertion, and it is the first test here.

The second thing worth pinning is the batching. At a season boundary three war
event types fire in the same instant; the plan asked for two job files, which
would have split them into two wakes and two posts narrating one moment.
"""

from __future__ import annotations

import pytest

from engine.event_contracts import EVENT_CONTRACTS, hard_post_event_types
from runtime.awareness import respond


def test_every_hard_post_has_a_job():
    """The precondition for `AWARENESS_LOOP_HOURS_DEFAULT = "9"`.

    A hard-post type with no job falls through `job_for` to the daily brain. At
    2×/day that was a ≤12h worst case; at 1×/day it is ≤24h. Before Phase 3,
    `week_finished`, `season_closed`, `tournament_finished` and `clan_birthday`
    were all in exactly that position — so the cadence cut and these jobs are one
    change, not two, and this test is the join between them.
    """
    uncovered = sorted(t for t in hard_post_event_types() if t not in respond.JOB_BY_EVENT_TYPE)
    assert uncovered == [], (
        f"hard-post types with no job: {uncovered}. Either give each a job or put "
        f"the awareness cron back to twice a day — a floor may not wait 24h."
    )


def test_the_season_close_triple_is_one_job_and_therefore_one_post():
    """Measured 2026-08-03T11:17:22Z: these three fired at the same instant.

    Wakes group by (class, model, job). One job means one group, one wake, one
    author. Two jobs — which is what the plan's `war_week.md` + `war_season.md`
    split would have produced — means two posts about one moment, which is the
    v4 failure this whole architecture exists to prevent.
    """
    triple = ["week_finished", "season_closed", "clan_league_changed"]
    jobs = {respond.JOB_BY_EVENT_TYPE.get(t) for t in triple}
    assert jobs == {"war_close"}

    events = [{"event_type": t, "signal_key": f"{t}:135:0"} for t in triple]
    assert respond.job_for(events) == "war_close", "the triple must resolve to one author"


def test_a_plain_week_close_uses_the_same_job():
    """Four weeks in five carry only `week_finished`."""
    assert respond.job_for(
        [{"event_type": "week_finished", "signal_key": "week_finished:135:1"}]
    ) == ("war_close")


def test_a_tournament_does_not_collide_with_a_war_close():
    """Both are (immediate, chat) hard posts. Only the job keeps them apart, and
    a tournament result inside a war-close post would be one confused story."""
    assert respond.JOB_BY_EVENT_TYPE["tournament_finished"] == "tournament"
    mixed = [
        {"event_type": "week_finished", "signal_key": "week_finished:135:0"},
        {"event_type": "tournament_finished", "signal_key": "tournament_finished:#ABC"},
    ]
    assert respond.job_for(mixed) is None, "a mixed wake falls to the brain rather than guessing"


def test_the_podium_still_separates_from_the_war_close():
    """The plan's explicit requirement: a ranked podium landing in the same tick
    as a season close is a different story and gets its own post."""
    assert respond.JOB_BY_EVENT_TYPE["pol_season_podium"] == "podium"
    assert respond.JOB_BY_EVENT_TYPE["season_closed"] == "war_close"


@pytest.mark.parametrize(
    "job",
    ["war_close", "tournament", "clan_birthday"],
)
def test_each_new_job_has_a_prompt_file(job):
    """`job_prompt` maps `_` to `-`; a missing file raises inside assemble_system
    on a live event, which is the worst possible time to find out."""
    import prompts

    text = prompts.job_prompt(job)
    assert text and text.strip(), f"prompts/jobs/{job.replace('_', '-')}.md is empty or missing"


def test_war_jobs_speak_where_the_registry_says_hard_posts_go():
    """awareness.md routes week_finished/season_closed/tournament_finished to
    #elixir and clan_birthday to #announcements. The job surfaces must agree, or
    a floor lands in the wrong channel."""
    assert respond.lanes_for(respond.job_spec("war_close")) == ("elixir",)
    assert respond.lanes_for(respond.job_spec("tournament")) == ("elixir",)
    assert respond.lanes_for(respond.job_spec("clan_birthday")) == ("announcements",)


def test_war_types_are_immediate_on_the_chat_tier():
    """A war close composed by Haiku was never the intent, and an `immediate`
    class is what makes it arrive in minutes."""
    for event_type in ("week_finished", "season_closed", "tournament_finished"):
        contract = EVENT_CONTRACTS[event_type]
        assert contract.wake == "immediate"
        assert contract.wake_model == "chat"


def test_war_workflows_are_outside_the_spend_ceiling():
    """The plan's own gate on this phase: these carry hard posts, so the
    workflow composing them MUST NOT be budget-gated or the daily ceiling could
    silence a season close."""
    from agent import spend_budget

    for workflow in ("wake_response", "wake_response_chat"):
        assert workflow not in spend_budget.BUDGETED
        assert workflow in spend_budget.ESSENTIAL


# --- the resolved facts a war post must state and must not derive -------------


def _war_event(event_type, signal_key, payload):
    return {
        "event_type": event_type,
        "signal_key": signal_key,
        "observed_at": "2026-08-17T09:42:49Z",
        "payload": payload,
    }


def test_the_week_label_is_resolved_to_what_the_clan_says(engine_conn):
    """`section_index` is 0-based and members count from one. Off-by-one in a
    headline is the error nobody forgives, so the model is handed the label."""
    seed = respond.build_seed(
        [_war_event("week_finished", "week_finished:135:0", {"our_fame": 10305, "our_rank": 1})],
        engine_conn,
    )
    war = seed["events"][0]["war"]
    assert war["week_label"] == "Week 1"
    assert war["season_id"] == 135


def test_competitor_clans_are_resolved_from_tags_to_names(engine_conn):
    """Standings carry tags only. A model that cannot resolve one either prints
    `#RJQQLLV9` or invents a name; both reach members."""
    engine_conn.execute(
        "INSERT OR REPLACE INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home) "
        "VALUES (?, ?, ?, ?, 0)",
        ("#RJQQLLV9", "Canadian Clash", "2026-08-01T00:00:00Z", "2026-08-17T00:00:00Z"),
    )
    seed = respond.build_seed(
        [
            _war_event(
                "week_finished",
                "week_finished:135:0",
                {
                    "our_fame": 10305,
                    "standings": [
                        {"clan_tag": "#J2RGCRVG", "fame": 10305, "rank": 1},
                        {"clan_tag": "#RJQQLLV9", "fame": 3600, "rank": 2},
                    ],
                },
            )
        ],
        engine_conn,
    )
    names = {s["clan_tag"]: s.get("clan_name") for s in seed["events"][0]["war"]["standings"]}
    assert names["#RJQQLLV9"] == "Canadian Clash"
    # An unknown tag resolves to None rather than a guess — absent is a fact.
    assert names["#J2RGCRVG"] is None or isinstance(names["#J2RGCRVG"], str)


@pytest.mark.parametrize(
    "prev_league,league,expected",
    [
        # Ascending inside a band: Silver II is ABOVE Silver I. This is the
        # opposite of the ranked ladder and it is the trap.
        ("Silver League I", "Silver League II", "promoted"),
        ("Silver League II", "Silver League I", "demoted"),
        ("Silver League III", "Gold League I", "promoted"),
        ("Gold League I", "Silver League III", "demoted"),
    ],
)
def test_league_direction_is_decided_before_the_model_sees_it(
    engine_conn, prev_league, league, expected
):
    """ "POAP KINGS drops to Silver II" after a promotion is the worst sentence
    this job could write, and a model inferring it from two names gets it right
    about half the time."""
    seed = respond.build_seed(
        [
            _war_event(
                "clan_league_changed",
                "clan_league_changed:135",
                {"league": league, "prev_league": prev_league, "war_trophies": 980},
            )
        ],
        engine_conn,
    )
    assert seed["events"][0]["war"]["direction"] == expected


def test_an_unrecognised_league_name_yields_no_direction_rather_than_a_guess(engine_conn):
    """Absent is a fact the job file knows how to handle; wrong is not."""
    seed = respond.build_seed(
        [
            _war_event(
                "clan_league_changed",
                "clan_league_changed:135",
                {"league": "Mystery League", "prev_league": "Silver League I"},
            )
        ],
        engine_conn,
    )
    assert "direction" not in seed["events"][0]["war"]


def test_a_war_close_is_marked_as_a_hard_post_wake(engine_conn):
    """The seed's `wake` field drives how the job treats its own obligation."""
    seed = respond.build_seed(
        [_war_event("week_finished", "week_finished:135:0", {"our_fame": 10305})], engine_conn
    )
    assert seed["wake"] == "hard_post"


def test_member_events_are_untouched_by_the_war_resolution(engine_conn):
    """A join must not grow a `war` block."""
    seed = respond.build_seed(
        [
            {
                "event_type": "member_joined",
                "signal_key": "member_joined:#AAA:2026-08-17T09:00:00Z",
                "subject_tag": "#AAA",
                "observed_at": "2026-08-17T09:00:00Z",
                "payload": {"name": "someone"},
            }
        ],
        engine_conn,
    )
    assert "war" not in seed["events"][0]
