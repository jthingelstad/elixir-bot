"""scripts/probe_llm.py must isolate telemetry by construction.

Ad-hoc harnesses written to answer a question about a model call have four times
written their stubbed results into the PRODUCTION telemetry database
(2026-07-10, 07-11, 08-03, 08-09), leaving 21 fabricated rows that skewed the
averages the table exists to produce.

The first fix attempted was a guard in `record_llm_call` that rejected
implausibly fast calls. That was the wrong layer: it made production code
responsible for detecting a developer-workflow mistake, using a heuristic
threshold that would silently drop real rows the day the API got faster. The
source of the problem is that `telemetry_path()` defaults to the production
file, so this tool exists to make the correct thing the easy thing — and these
tests pin the property that makes it correct.
"""

from __future__ import annotations

import ast
import pathlib

PROBE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "probe_llm.py"


def _module():
    return ast.parse(PROBE.read_text())


def test_the_telemetry_override_is_set_before_any_project_import():
    """Ordering is the whole mechanism. agent.core reaches telemetry through
    storage, and a late override leaves the first connection pointed at
    production — so the assignment has to precede every project import."""
    tree = _module()
    override_line = None
    first_project_import = None

    for node in ast.walk(tree):
        if override_line is None and isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "environ"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "ELIXIR_TELEMETRY_DB_PATH"
                ):
                    override_line = node.lineno
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            module = getattr(node, "module", None) or ""
            if any(
                n.startswith(("agent", "storage", "runtime", "elixir_agent", "db"))
                for n in names + [module]
            ):
                if first_project_import is None or node.lineno < first_project_import:
                    first_project_import = node.lineno

    assert override_line is not None, "the probe must override ELIXIR_TELEMETRY_DB_PATH"
    assert first_project_import is not None, "expected the probe to import the agent package"
    assert override_line < first_project_import, (
        f"the override is on line {override_line} but a project import is on "
        f"{first_project_import}; telemetry would open against production first"
    )


def test_the_probe_verifies_its_own_isolation_at_runtime():
    """Static ordering is not enough — an indirect import could still win, so the
    tool checks the resolved path and refuses rather than writing a fake row."""
    source = PROBE.read_text()
    assert "_assert_isolated" in source
    assert "refusing to run" in source


def test_the_probe_does_not_post_to_discord():
    """It calls composers, never the jobs that deliver their output."""
    source = PROBE.read_text()
    for forbidden in ("deliver_posts", "post_to_discord", "_post_to_elixir", "send_email"):
        assert forbidden not in source, f"probe must not reach {forbidden}"
