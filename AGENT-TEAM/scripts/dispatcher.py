#!/usr/bin/env python3
"""Read-only selector and handoff prompt for app-visible AGENT-TEAM tasks.

This program never claims an issue, invokes Codex, creates a task, or mutates the
repository. GitHub is the durable queue; an active Codex project conversation
performs preflight, claims the selected issue, and creates one visible role task.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "dispatch.toml"


class DispatchError(RuntimeError):
    """Fail-closed routing or configuration error."""


@dataclass(frozen=True)
class Route:
    label: str
    role: str
    role_file: Path
    title: str
    model: str
    reasoning_effort: str
    priority: int


@dataclass(frozen=True)
class InferenceRule:
    route: str
    all_labels: frozenset[str]
    any_labels: frozenset[str]
    no_labels: frozenset[str]


@dataclass(frozen=True)
class Config:
    path: Path
    repo: str
    # `cwd` is a deployment setting: the absolute directory a dispatched agent is
    # told to work in on the operator's machine. `repo_root` is where THIS config
    # was read from. They are the same on the operator's box and differ anywhere
    # else (CI, another checkout), so role files -- which ship in the repo -- are
    # resolved against repo_root; only the agent's working directory uses cwd.
    cwd: Path
    repo_root: Path
    dispatch_prefix: str
    serial_claim_label: str
    stop_labels: frozenset[str]
    pending_labels: frozenset[str]
    routes: dict[str, Route]
    inference: tuple[InferenceRule, ...]


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    labels: frozenset[str]
    created_at: str
    updated_at: str
    url: str
    body: str = ""

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Issue:
        raw_labels = value.get("labels", [])
        labels = frozenset(
            item["name"] if isinstance(item, dict) else str(item) for item in raw_labels
        )
        return cls(
            number=int(value["number"]),
            title=str(value["title"]),
            state=str(value.get("state", "OPEN")).upper(),
            labels=labels,
            created_at=str(value.get("createdAt", "")),
            updated_at=str(value.get("updatedAt", "")),
            url=str(value.get("url", "")),
            body=str(value.get("body", "")),
        )


@dataclass(frozen=True)
class Selection:
    issue: Issue
    route: Route
    source: str


@dataclass(frozen=True)
class Transition:
    valid: bool
    outcome: str
    next_route: str | None = None


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    cwd = Path(raw["cwd"]).resolve()
    repo_root = path.resolve().parents[1]
    routes: dict[str, Route] = {}
    for item in raw.get("routes", []):
        label = str(item["label"])
        if label in routes:
            raise DispatchError(f"duplicate route label: {label}")
        routes[label] = Route(
            label=label,
            role=str(item["role"]),
            role_file=repo_root / str(item["role_file"]),
            title=str(item["title"]),
            model=str(item["model"]),
            reasoning_effort=str(item["reasoning_effort"]),
            priority=int(item["priority"]),
        )
    inference = tuple(
        InferenceRule(
            route=str(item["route"]),
            all_labels=frozenset(item.get("all", [])),
            any_labels=frozenset(item.get("any", [])),
            no_labels=frozenset(item.get("none", [])),
        )
        for item in raw.get("inference", [])
    )
    return Config(
        path=path.resolve(),
        repo=str(raw["repo"]),
        cwd=cwd,
        repo_root=repo_root,
        dispatch_prefix=str(raw["dispatch_prefix"]),
        serial_claim_label=str(raw["serial_claim_label"]),
        stop_labels=frozenset(raw["stop_labels"]),
        pending_labels=frozenset(raw["pending_labels"]),
        routes=routes,
        inference=inference,
    )


def _run_json(args: Sequence[str], *, cwd: Path) -> Any:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DispatchError(f"command failed ({completed.returncode}): {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError("command returned invalid JSON") from exc


class GitHub:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.gh = shutil.which("gh")
        if not self.gh:
            raise DispatchError("gh CLI is not available")

    def list_open(self) -> list[Issue]:
        raw = _run_json(
            [
                self.gh,
                "issue",
                "list",
                "--repo",
                self.config.repo,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,state,labels,createdAt,updatedAt,url,body",
            ],
            cwd=self.config.cwd,
        )
        return [Issue.from_json(item) for item in raw]

    def view(self, number: int) -> Issue:
        raw = _run_json(
            [
                self.gh,
                "issue",
                "view",
                str(number),
                "--repo",
                self.config.repo,
                "--json",
                "number,title,state,labels,createdAt,updatedAt,url,body",
            ],
            cwd=self.config.cwd,
        )
        return Issue.from_json(raw)

    def label_names(self) -> set[str]:
        raw = _run_json(
            [
                self.gh,
                "label",
                "list",
                "--repo",
                self.config.repo,
                "--limit",
                "300",
                "--json",
                "name",
            ],
            cwd=self.config.cwd,
        )
        return {str(item["name"]) for item in raw}


def _rule_matches(rule: InferenceRule, labels: frozenset[str]) -> bool:
    return (
        rule.all_labels <= labels
        and (not rule.any_labels or bool(rule.any_labels & labels))
        and not rule.no_labels & labels
    )


def infer_route(issue: Issue, config: Config) -> tuple[Route, str] | None:
    if issue.state != "OPEN" or config.stop_labels & issue.labels:
        return None

    explicit = sorted(label for label in issue.labels if label.startswith(config.dispatch_prefix))
    if len(explicit) > 1:
        raise DispatchError(
            f"issue #{issue.number} has multiple dispatch labels: {', '.join(explicit)}"
        )
    if explicit:
        label = explicit[0]
        route = config.routes.get(label)
        if route is None:
            raise DispatchError(f"issue #{issue.number} has unknown dispatch label: {label}")
        return route, "explicit"

    matches = [rule for rule in config.inference if _rule_matches(rule, issue.labels)]
    if not matches:
        return None
    unknown = sorted({rule.route for rule in matches if rule.route not in config.routes})
    if unknown:
        raise DispatchError(f"inference references unknown routes: {', '.join(unknown)}")
    route = min((config.routes[rule.route] for rule in matches), key=lambda item: item.priority)
    return route, "inferred"


def active_claims(issues: Sequence[Issue], config: Config) -> list[Issue]:
    return sorted(
        (
            issue
            for issue in issues
            if issue.state == "OPEN" and config.serial_claim_label in issue.labels
        ),
        key=lambda issue: (issue.updated_at, issue.number),
    )


def select_candidates(issues: Sequence[Issue], config: Config) -> list[Selection]:
    # One active claim owns the shared checkout. Even read-only roles may write a
    # report or issue artifact, so the selector serializes the whole team.
    if active_claims(issues, config):
        return []

    selected: list[Selection] = []
    for issue in issues:
        inferred = infer_route(issue, config)
        if inferred is None:
            continue
        route, source = inferred
        selected.append(Selection(issue=issue, route=route, source=source))
    return sorted(
        selected,
        key=lambda item: (
            item.route.priority,
            0 if item.source == "explicit" else 1,
            item.issue.created_at,
            item.issue.number,
        ),
    )


def assess_transition(current_route: str, issue: Issue, config: Config) -> Transition:
    routes = sorted(label for label in issue.labels if label.startswith(config.dispatch_prefix))
    if current_route in routes:
        return Transition(False, "current dispatch label remains")
    if config.serial_claim_label in issue.labels:
        return Transition(False, "serial claim remains")
    if len(routes) > 1:
        return Transition(False, "multiple next dispatch labels")
    if issue.state == "CLOSED":
        if routes:
            return Transition(False, "closed issue still has a next dispatch label")
        return Transition(True, "closed")
    if config.stop_labels & issue.labels:
        if routes:
            return Transition(False, "human stop state also has a dispatch label")
        return Transition(True, "human-stop")
    if len(routes) == 1:
        if routes[0] not in config.routes:
            return Transition(False, "unknown next dispatch label")
        return Transition(True, "handoff", routes[0])
    return Transition(False, "open issue has no next dispatch or human stop state")


def build_handoff_prompt(issue: Issue, route: Route, config: Config) -> str:
    if config.serial_claim_label not in issue.labels:
        raise DispatchError(
            f"issue #{issue.number} is not claimed with `{config.serial_claim_label}`"
        )
    if route.label not in issue.labels:
        raise DispatchError(f"issue #{issue.number} is not explicitly labeled `{route.label}`")
    other_routes = sorted(
        label
        for label in issue.labels
        if label.startswith(config.dispatch_prefix) and label != route.label
    )
    if other_routes:
        raise DispatchError(f"issue #{issue.number} has multiple dispatch labels")

    title = f"#{issue.number} {route.title}"
    return f"""Create one normal local Codex project task in `{config.cwd}` titled `{title}`.
Do not use a subagent, `codex exec`, launchd, or an ephemeral/background shell run.

Pass this role prompt to the new task:

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, `AGENT-TEAM/README.md`, and
`{route.role_file.relative_to(config.repo_root)}` completely, in that order, then execute the role
for [{issue.title}]({issue.url}) (issue #{issue.number}). Your identity is `{route.role}`.
The initiating conversation already ran preflight and added `{config.serial_claim_label}` plus
`{route.label}`. Re-read authoritative issue state and rerun preflight before acting. Accept this
specific claim as yours; do not skip it merely because `{config.serial_claim_label}` is present.
If another claim or unsafe checkout state is now present, stop without mutation and report it.

Do exactly one focused issue-scoped run. Preserve the approval gate, commit lane, deployment
ownership, and the rule that no pre-existing commit may be pushed. Use `{title}` as the base task
title, add only short phase suffixes, and finish with `✓` only after making a valid authoritative
transition. Before the final response, remove `{config.serial_claim_label}` and `{route.label}`,
then either close the issue, leave it in `proposal`/`blocked`/`needs-design`, or add exactly one
next `dispatch:*` label. Never invoke the next role directly; a later active conversation creates
that visible task.
"""


def _config_problems(config: Config) -> list[str]:
    problems: list[str] = []
    if not config.cwd.is_dir():
        problems.append(f"missing cwd: {config.cwd}")
    if not config.routes:
        problems.append("no routes configured")
    if len({route.priority for route in config.routes.values()}) != len(config.routes):
        problems.append("route priorities must be unique")
    if len({route.role for route in config.routes.values()}) != len(config.routes):
        problems.append("roles must be unique")
    for route in config.routes.values():
        if not route.label.startswith(config.dispatch_prefix):
            problems.append(f"route outside dispatch prefix: {route.label}")
        if not route.role_file.is_file():
            problems.append(f"missing role file: {route.role_file}")
    for rule in config.inference:
        if rule.route not in config.routes:
            problems.append(f"inference references unknown route: {rule.route}")
    return problems


def check_config(config: Config, *, live: bool = False) -> int:
    problems = _config_problems(config)
    if live and not problems:
        try:
            actual = GitHub(config).label_names()
            expected = set(config.routes) | set(config.pending_labels) | set(config.stop_labels)
            expected.add(config.serial_claim_label)
            missing = sorted(expected - actual)
            if missing:
                problems.append(f"missing GitHub labels: {', '.join(missing)}")
        except DispatchError as exc:
            problems.append(f"live label check failed: {exc}")
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print(f"PASS dispatcher config: {len(config.routes)} routes, {len(config.inference)} rules")
    if live:
        print("PASS GitHub dispatch labels")
    return 0


def shadow(config: Config, *, show_all: bool = False) -> int:
    issues = GitHub(config).list_open()
    claims = active_claims(issues, config)
    if claims:
        for issue in claims:
            print(f"Active claim blocks dispatch: #{issue.number} {issue.title}")
        return 0
    candidates = select_candidates(issues, config)
    visible = candidates if show_all else candidates[:1]
    if not visible:
        print("No actionable issues.")
        return 0
    for item in visible:
        print(
            json.dumps(
                {
                    "issue": item.issue.number,
                    "title": item.issue.title,
                    "route": item.route.label,
                    "role": item.route.role,
                    "role_file": str(item.route.role_file.relative_to(config.repo_root)),
                    "task_title": f"#{item.issue.number} {item.route.title}",
                    "model": item.route.model,
                    "reasoning_effort": item.route.reasoning_effort,
                    "source": item.source,
                    "priority": item.route.priority,
                },
                sort_keys=True,
            )
        )
    return 0


def handoff(config: Config, issue_number: int) -> int:
    issue = GitHub(config).view(issue_number)
    routed = infer_route(issue, config)
    if routed is None or routed[1] != "explicit":
        raise DispatchError(f"issue #{issue.number} must have exactly one explicit dispatch label")
    route, _source = routed
    print(build_handoff_prompt(issue, route, config))
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="validate local configuration")
    parser.add_argument("--live", action="store_true", help="include GitHub labels in --check")
    parser.add_argument("--shadow", action="store_true", help="print read-only routing")
    parser.add_argument("--all", action="store_true", help="show all shadow candidates")
    parser.add_argument("--handoff", type=int, metavar="ISSUE", help="print a claimed role prompt")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    if args.check:
        return check_config(config, live=args.live)
    if args.shadow:
        return shadow(config, show_all=args.all)
    if args.handoff is not None:
        return handoff(config, args.handoff)
    print(
        "Automatic role launch does not exist. Use --shadow to select work, claim it from an "
        "active Codex project conversation, then use --handoff ISSUE to create one visible task.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DispatchError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"dispatcher error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
