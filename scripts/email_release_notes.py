"""Email Elixir's release notes (the detailed tier) via Fastmail JMAP.

    ./venv/bin/python scripts/email_release_notes.py --dry-run
    ./venv/bin/python scripts/email_release_notes.py --to jamie@thingelstad.com

Generates the first-person release-notes draft (same machinery cut_release.py
uses) and emails it from elixir@poapkings.com. Scope defaults to everything since
the latest release tag; --days / --since override. --dry-run prints the subject +
body and sends nothing.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from agent import release_notes as rn  # noqa: E402
from agent.mail import outbound  # noqa: E402

DEFAULT_TO = os.getenv("ELIXIR_RELEASE_EMAIL_TO", "jamie@thingelstad.com")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", default=DEFAULT_TO, help=f"recipient (default: {DEFAULT_TO})")
    parser.add_argument("--days", type=int, default=None, help="scope: look back N days")
    parser.add_argument("--since", metavar="REF", help="scope: changes since this ref")
    parser.add_argument("--dry-run", action="store_true", help="print, send nothing")
    args = parser.parse_args()

    if not args.dry_run and not outbound.enabled():
        print("FASTMAIL_JMAP_TOKEN not configured — cannot send.")
        return 2

    draft = rn.release_notes_draft(days=args.days, since_ref=args.since)
    if draft is None:
        print("No changes in the window — nothing to email.")
        return 0

    name = draft.get("release_name")
    subject = draft["subject"]
    if name and name.lower() not in subject.lower():
        subject = f'{name} — {subject}'
    print(f"Release name: {name or '(nameless)'}")
    print(f"Window: {draft['window']}")
    print(f"Subject: {subject}\n")

    if args.dry_run:
        print("=" * 72)
        print(draft["body"])
        print("=" * 72)
        print(f"\n[dry-run] would email to {args.to} from {os.getenv('ELIXIR_EMAIL_ADDRESS')}")
        return 0

    result = outbound.send(to=args.to, subject=subject, body=draft["body"])
    print(f"Sent to {result['to']} — emailId {result.get('emailId')} · "
          f"submissionId {result.get('submissionId')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
