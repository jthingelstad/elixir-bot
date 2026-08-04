#!/bin/sh
# The gate list. ONE definition, run by both CI and the local pre-commit hook.
#
# This file exists because the two lists drifted. The hook carried a comment
# claiming it matched .github/workflows/tests.yml; it matched every step except
# "Audit locked dependencies". On 2026-08-03 pip-audit found three CVEs in
# aiohttp 3.14.1, and three consecutive pushes were committed cleanly, pushed
# cleanly, and failed in CI ~20 seconds later. A comment asserting parity is not
# parity — the only way two lists stay identical is to be one list.
#
# CI runs this. The hook runs this. Adding a gate here adds it to both.
# tests/test_ci_local_parity.py fails if either caller stops using it.
#
# Ordered fail-fast: cheapest and most likely to fail first.

set -e

if ! command -v uv >/dev/null 2>&1; then
    echo "gates: uv not found; install uv first" >&2
    exit 1
fi

run_gate() {
    label="$1"
    shift
    echo "gate: $label"
    if ! "$@"; then
        echo "" >&2
        echo "gate: $label FAILED — fix the above." >&2
        echo "(local commit only: 'git commit --no-verify' bypasses, but CI will not)" >&2
        exit 1
    fi
}

run_gate "dependency lock" uv lock --check

# The gate that was missing locally. Needs network: a new CVE disclosure can
# turn this red with no code change at all, which is exactly how it should
# behave — but it means an offline machine cannot clear it. That is a real
# failure, not a false one: the push would fail in CI too.
audit_deps() {
    uv export --locked --no-dev --no-emit-project --format requirements-txt >"${TMPDIR:-/tmp}/gates-requirements.txt" &&
        uvx --from pip-audit==2.10.1 pip-audit \
            --requirement "${TMPDIR:-/tmp}/gates-requirements.txt" --disable-pip
}
run_gate "dependency audit (CVEs)" audit_deps

run_gate "documentation" uv run --locked python scripts/check_docs.py
run_gate "exception policy" uv run --locked python scripts/check_exception_hygiene.py
run_gate "ruff check" uv run --locked ruff check .
run_gate "ruff format" uv run --locked ruff format --check .
run_gate "capability contracts" uv run --locked mypy capabilities/
run_gate "tests with capability coverage" uv run --locked pytest tests/ -q \
    --cov=capabilities --cov-report=term-missing --cov-fail-under=80

# The only full-pipeline assertion in the repo: drives production run_tick
# through a synthetic 7-day war week (336 ticks, skewed reset, mid-week joiner
# and leaver) and asserts 14 gates incl. the DB invariants. ~1s, no network,
# no LLM, no live DB. Unit tests pass with the pipeline broken; this does not.
run_gate "war-week simulation" uv run --locked python scripts/simulate.py

echo "gates: all passed"
