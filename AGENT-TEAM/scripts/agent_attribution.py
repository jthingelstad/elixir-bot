#!/usr/bin/env python3
"""Attribute AGENT-TEAM commits and issue comments to a rostered role."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO / "AGENT-TEAM" / "automations.toml"


def _identities() -> dict[str, str]:
    plan = tomllib.loads(PLAN_PATH.read_text())
    return {entry["id"]: entry["name"] for entry in plan["automation"]}


def _name(automation_id: str) -> str:
    identities = _identities()
    try:
        return identities[automation_id]
    except KeyError as exc:
        choices = ", ".join(sorted(identities))
        raise SystemExit(
            f"unknown automation id {automation_id!r}; choose one of: {choices}"
        ) from exc


def _signature(name: str) -> str:
    return f"— **{name}** (agent)"


def _signed_body(body: str, name: str) -> str:
    body = body.rstrip()
    signature = _signature(name)
    if body.endswith(signature):
        return body
    return f"{body}\n\n{signature}"


def _git_email() -> str:
    proc = subprocess.run(
        ["git", "config", "--get", "user.email"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    email = proc.stdout.strip()
    if not email:
        raise SystemExit("git user.email is empty; configure a real verified email first")
    return email


def _commit(automation_id: str, git_args: list[str]) -> int:
    name = _name(automation_id)
    if git_args and git_args[0] == "--":
        git_args = git_args[1:]
    if not git_args:
        raise SystemExit("commit requires git commit arguments after --")
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = _git_email()
    return subprocess.run(["git", "commit", *git_args], cwd=REPO, env=env, check=False).returncode


def _issue_comment(automation_id: str, issue: str, body: str) -> int:
    name = _name(automation_id)
    signed = _signed_body(body, name)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
        handle.write(signed)
        handle.flush()
        return subprocess.run(
            ["gh", "issue", "comment", issue, "--body-file", handle.name],
            cwd=REPO,
            check=False,
        ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    identity = subparsers.add_parser("identity", help="print a rostered display name")
    identity.add_argument("automation_id")

    commit = subparsers.add_parser("commit", help="run git commit with the role as author")
    commit.add_argument("automation_id")
    commit.add_argument("git_args", nargs=argparse.REMAINDER)

    comment = subparsers.add_parser("issue-comment", help="post a signed GitHub issue comment")
    comment.add_argument("automation_id")
    comment.add_argument("issue")
    body_group = comment.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body")
    body_group.add_argument("--body-file", type=Path)

    args = parser.parse_args()
    if args.command == "identity":
        print(_name(args.automation_id))
        return 0
    if args.command == "commit":
        return _commit(args.automation_id, args.git_args)
    body = args.body if args.body is not None else args.body_file.read_text()
    return _issue_comment(args.automation_id, args.issue, body)


if __name__ == "__main__":
    sys.exit(main())
