#!/usr/bin/env python3
"""Verify that the production lock satisfies every direct runtime requirement."""

from __future__ import annotations

from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _locked_versions() -> dict[str, Version]:
    locked: dict[str, Version] = {}
    for line in _requirement_lines(LOCK):
        if "==" not in line:
            raise SystemExit(f"{LOCK.name}: expected an exact pin, got {line!r}")
        name, version = line.split("==", 1)
        locked[canonicalize_name(name)] = Version(version)
    return locked


def main() -> int:
    locked = _locked_versions()
    findings: list[str] = []

    for line in _requirement_lines(REQUIREMENTS):
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            findings.append(f"invalid direct requirement {line!r}: {exc}")
            continue

        name = canonicalize_name(requirement.name)
        version = locked.get(name)
        if version is None:
            findings.append(f"{requirement.name} is missing from {LOCK.name}")
        elif requirement.specifier and version not in requirement.specifier:
            findings.append(
                f"{requirement.name}=={version} does not satisfy "
                f"{requirement.specifier}"
            )

    if findings:
        print("Dependency lock is inconsistent:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print(
        f"Dependency lock is consistent: {len(locked)} production packages, "
        f"{len(_requirement_lines(REQUIREMENTS))} direct requirements."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
