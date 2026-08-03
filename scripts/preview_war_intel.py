#!/usr/bin/env python3
"""Preview (or test-send) the Clan Wars Intel Report.

Default prints the markdown to stdout and sends nothing. Mirrors
scripts/preview_member_report.py.

    uv run python scripts/preview_war_intel.py                       # markdown, no send
    uv run python scripts/preview_war_intel.py --facts               # the model brief only
    uv run python scripts/preview_war_intel.py --html /tmp/intel.html
    uv run python scripts/preview_war_intel.py --to jamie@thingelstad.com
    uv run python scripts/preview_war_intel.py --broadcast           # every verified member

`--no-llm` skips the model entirely and renders the deterministic fallback copy,
which is the fastest way to check the tables and stat lines.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

# email_jmap reads its config at import time, so the env must land first.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def _latest_season() -> int | None:
    import db

    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT season_id FROM war_seasons ORDER BY season_id DESC LIMIT 1"
        ).fetchone()
    return int(row[0]) if row else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, help="season id (default: latest in war_seasons)")
    ap.add_argument("--facts", action="store_true", help="print the model brief and exit")
    ap.add_argument("--no-llm", action="store_true", help="render deterministic copy only")
    ap.add_argument("--html", help="write the rendered HTML here instead of sending")
    ap.add_argument("--to", help="send to ONE address (test)")
    ap.add_argument("--broadcast", action="store_true", help="send to every verified member")
    args = ap.parse_args()

    from runtime import war_intel

    season = args.season or _latest_season()
    ctx = war_intel.build_intel_context(season_id=season)
    facts = war_intel.facts_for_model(ctx)
    if args.facts:
        print(facts)
        return 0

    narrative = {}
    if not args.no_llm:
        from agent.workflows import generate_war_intel_narrative

        narrative = generate_war_intel_narrative(facts) or {}
        if not narrative:
            print("!! model returned nothing; falling back to deterministic copy", file=sys.stderr)

    subject, body = war_intel.render_war_intel_email(ctx, narrative)

    if args.html:
        from agent.mail import email_render

        with open(args.html, "w") as fh:
            fh.write(email_render.text_to_html(body))
        print(f"wrote {args.html}")
        return 0

    if not args.to and not args.broadcast:
        print(f"Subject: {subject}\n")
        print(body)
        return 0

    from agent.mail import outbound

    if not outbound.enabled():
        print("mail is not configured (FASTMAIL_JMAP_TOKEN missing)", file=sys.stderr)
        return 1

    if args.to:
        outbound.send(to=args.to, subject=subject, body=body)
        print(f"sent to {args.to}")
        return 0

    import db

    recipients = [m["email"] for m in db.list_member_emails()]
    if not recipients:
        print("no members with a verified email", file=sys.stderr)
        return 1
    sender = os.getenv("ELIXIR_EMAIL_ADDRESS", "elixir@poapkings.com")
    outbound.send(to=sender, bcc=recipients, subject=subject, body=body)
    print(f"broadcast to {len(recipients)} member(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
