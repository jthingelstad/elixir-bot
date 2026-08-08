"""A standing deliverable that produces nothing is a FAILURE, not a success.

2026-08-03: the Monday clan report never sent. The job status was green, the LLM
call was ok=1, and the only evidence anywhere was `completion_chars=0` in a log
line. Cause: weekly_recap ran on claude-opus-5 with max_tokens=1600 and spent
the entire budget on extended thinking (stop_reason=max_tokens,
completion_tokens=1600), so the composer returned an empty string — and the job
recorded `mark_job_success("no recap generated")`.

The distinction this file protects: "nothing to do" is success (no members with
a verified email, no season owed a report); "I was asked to compose and produced
nothing" is failure. Only a human noticing an absent email caught it otherwise.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
JOBS = ROOT / "runtime/jobs"

# Phrases that mean the composer ran and returned nothing. These must never be
# paired with mark_job_success.
COMPOSER_EMPTY = re.compile(
    r"mark_job_success\([^)]*?(no recap generated|no report generated|"
    r"no promotion content|no promotion channel copy)",
    re.IGNORECASE | re.DOTALL,
)


def test_no_job_reports_success_for_an_empty_composition():
    offenders = []
    for path in sorted(JOBS.glob("*.py")):
        for match in COMPOSER_EMPTY.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:80]}")
    assert not offenders, (
        f"a composer that produced nothing must mark_job_failure so it surfaces; found: {offenders}"
    )


def test_weekly_recap_has_room_to_think():
    """max_tokens must cover thinking AND the answer, not just the answer.

    Reads the policy rather than scraping the source: since 2026-08-08 the
    ceiling lives in agent.core.MODEL_CALL_POLICY, which is both where it is
    actually chosen and a far less brittle thing to assert against than a regex
    over a file that happened to contain the number."""
    import agent.core as core

    assert core.policy_for("weekly_recap").max_tokens >= 8192, (
        "1600 was exhausted entirely by extended thinking on claude-opus-5, "
        "yielding an empty recap and a silently missing weekly report"
    )
