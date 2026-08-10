#!/usr/bin/env python3
"""Operator controls for durable scheduled-job periods."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import db  # noqa: E402
from runtime import status as runtime_status  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    skip = subparsers.add_parser("skip", help="record an intentional period skip")
    skip.add_argument("job_name")
    skip.add_argument("period_key")
    skip.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "skip":
        runtime_status.mark_job_period_skipped(args.job_name, args.period_key, args.reason)
        state = db.list_runtime_job_status().get(args.job_name) or {}
        if state.get("last_skipped_period") != args.period_key:
            raise RuntimeError(
                f"skip receipt did not persist for {args.job_name}/{args.period_key}"
            )
        print(f"recorded skip: {args.job_name} {args.period_key}")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
