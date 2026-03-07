#!/usr/bin/env python3
"""Export or import Elixir member metadata as CSV.

Usage:
    venv/bin/python scripts/member_metadata_csv.py export --out member-metadata.csv
    venv/bin/python scripts/member_metadata_csv.py import --in member-metadata.csv
    venv/bin/python scripts/member_metadata_csv.py import --in member-metadata.csv --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export or import Elixir member metadata CSV")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Write a member metadata CSV")
    export_parser.add_argument("--out", required=True, help="Output CSV path")
    export_parser.add_argument(
        "--status",
        choices=["active", "left", "all"],
        default="active",
        help="Member status filter for export",
    )

    import_parser = subparsers.add_parser("import", help="Apply a member metadata CSV")
    import_parser.add_argument("--in", dest="input_path", required=True, help="Input CSV path")
    import_parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing changes")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "export":
        status = None if args.status == "all" else args.status
        count = db.export_member_metadata_csv(args.out, status=status)
        print(json.dumps({"command": "export", "rows_written": count, "path": args.out}, indent=2))
        return 0

    result = db.import_member_metadata_csv(args.input_path, dry_run=args.dry_run)
    print(
        json.dumps(
            {
                "command": "import",
                "path": args.input_path,
                "dry_run": args.dry_run,
                **result,
            },
            indent=2,
        )
    )
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
