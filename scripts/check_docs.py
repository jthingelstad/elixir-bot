#!/usr/bin/env python3
"""Check portable Markdown links and current-runbook architecture vocabulary."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
URI_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

# These names describe deleted production paths. They are allowed in archived
# and historical design documents, but not in current operator/agent guidance.
RETIRED_GUIDANCE_TERMS = {
    "README.md": (
        "v5-reactive-tick",
        "`war-poll`",
        "`player-progression`",
        "`award-detection`",
        "event_core/",
        "heartbeat.py",
        "`site-content`",
        "scripts/upgrade.sh",
        "elixir-v5.db",
    ),
    "SETUP.md": (
        "v5-reactive-tick",
        "`war-poll`",
        "`player-progression`",
        "`award-detection`",
        "event_core/",
        "heartbeat.py",
        "`site-content`",
        "scripts/upgrade.sh",
        "elixir-v5.db",
    ),
    "AGENT-TEAM/README.md": (
        "event_core/",
        "elixir-v5.db",
        "elixir-v5-events.db",
        "v5-reactive-tick",
    ),
    "AGENT-TEAM/data-analyst.md": (
        "event_core/",
        "elixir-v5.db",
        "elixir-v5-events.db",
        "battle_telemetry",
        "`detections`",
    ),
    "AGENT-TEAM/quality-manager.md": (
        "event_core.live",
        "elixir-v5.db",
        "elixir-v5-events.db",
        "battle_telemetry",
        "`detections`",
    ),
    "AGENT-TEAM/operations-manager.md": (
        "event_core.live",
        "elixir-v5.db",
        "elixir-v5-events.db",
    ),
    ".claude/skills/awareness-report/SKILL.md": (
        "/Users/jamie",
        "`elixir.db`",
    ),
    ".claude/skills/cr-api-doc-audit/SKILL.md": (
        "/Users/jamie",
        "`elixir.db`",
        "180 days",
    ),
    ".claude/skills/llm-cost-report/SKILL.md": (
        "/Users/jamie",
        "`elixir.db`",
    ),
    ".claude/skills/log-triage/SKILL.md": (
        "/Users/jamie",
        "`elixir.log`",
    ),
}


def _tracked_markdown() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.md",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line and (ROOT / line).exists()]


def _link_path(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    return unquote(target.split("#", 1)[0])


def _link_findings(path: Path) -> list[str]:
    findings: list[str] = []
    in_fence = False
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in MARKDOWN_LINK.finditer(line):
            target = _link_path(match.group(1))
            if not target or target.startswith("//") or URI_SCHEME.match(target):
                continue
            relative = path.relative_to(ROOT)
            if Path(target).is_absolute():
                findings.append(
                    f"{relative}:{line_number}: absolute local link is not portable: {target}"
                )
                continue
            destination = (path.parent / target).resolve()
            if not destination.exists():
                findings.append(f"{relative}:{line_number}: missing local link target: {target}")
    return findings


def main() -> int:
    findings: list[str] = []
    markdown_files = _tracked_markdown()
    for path in markdown_files:
        findings.extend(_link_findings(path))

    for relative, retired_terms in RETIRED_GUIDANCE_TERMS.items():
        text = (ROOT / relative).read_text()
        for term in retired_terms:
            if term in text:
                findings.append(f"{relative}: retired production term remains: {term}")

    if findings:
        print("Documentation checks failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(f"Documentation checks passed for {len(markdown_files)} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
