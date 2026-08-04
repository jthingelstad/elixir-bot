"""Email Elixir's release notes (the detailed tier) via Fastmail JMAP.

    uv run python scripts/email_release_notes.py --dry-run
    uv run python scripts/email_release_notes.py --to jamie@thingelstad.com

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

import db  # noqa: E402
from agent import release_notes as rn  # noqa: E402
from agent.mail import outbound  # noqa: E402

EMAIL_ADDRESS = os.getenv("ELIXIR_EMAIL_ADDRESS", "elixir@poapkings.com")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--to",
        default=None,
        help="override: email ONLY this address (testing). Default broadcasts "
        "to every clan member with a verified email.",
    )
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
        subject = f"{name} — {subject}"
    recipients = [args.to] if args.to else [m["email"] for m in db.list_member_emails()]
    print(f"Release name: {name or '(nameless)'}")
    print(f"Window: {draft['window']}")
    print(f"Subject: {subject}")
    print(
        f"Recipients: {len(recipients)} "
        f"({'override' if args.to else 'clan members with a verified email'})\n"
    )

    if args.dry_run:
        print("=" * 72)
        print(draft["body"])
        print("=" * 72)
        print(f"\n[dry-run] would email to {len(recipients)} recipient(s) from {EMAIL_ADDRESS}")
        return 0

    if not recipients:
        print("No clan members have a verified email on file — nothing to send.")
        return 0
    from runtime import email_dedup

    # One broadcast per release. A --to override is a manual test send, never deduped.
    key = str(name or subject)
    if not args.to and email_dedup.already_sent("release_notes", key):
        print(f"Release notes for {key} were already emailed — nothing sent.")
        return 0
    outbound.send(to=EMAIL_ADDRESS, bcc=recipients, subject=subject, body=draft["body"])
    print(f"Sent to {len(recipients)} recipient(s) (bcc) from {EMAIL_ADDRESS}.")
    if not args.to and not email_dedup.record_sent(
        "release_notes", key, detail=f"{len(recipients)} recipient(s)"
    ):
        print("⚠️  Sent but NOT recorded — a re-run would email again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
