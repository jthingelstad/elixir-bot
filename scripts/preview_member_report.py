"""Preview one member's Arena Dispatch (the personalized weekly report).

    ./venv/bin/python scripts/preview_member_report.py --tag '#20JJJ2CCRU'
    ./venv/bin/python scripts/preview_member_report.py --tag '#X' --html /tmp/report.html
    ./venv/bin/python scripts/preview_member_report.py --tag '#X' --to jamie@thingelstad.com

Builds the fact context, generates the narrative, renders the email, and either
prints it, writes the HTML, or sends a single test email. The tuning tool for the
'fun to read' bar — no schedule, one member at a time.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv("/Users/otto/Projects/elixir-bot/.env")

import db  # noqa: E402
from runtime import member_report  # noqa: E402


def _member_name(tag: str) -> str:
    row = db.get_connection().execute(
        "SELECT current_name FROM players WHERE player_tag = ?", (tag,)).fetchone()
    return (row["current_name"] if row else None) or tag


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", required=True, help="player tag, e.g. '#20JJJ2CCRU'")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--now", default=None, help="pin window end (ISO) for determinism")
    ap.add_argument("--facts", action="store_true", help="print only the model facts brief")
    ap.add_argument("--html", metavar="PATH", help="write the rendered HTML email here")
    ap.add_argument("--to", metavar="ADDR", help="send a single test email to this address")
    args = ap.parse_args()

    tag = args.tag if args.tag.startswith("#") else f"#{args.tag}"
    name = _member_name(tag)
    ctx = member_report.build_member_report_context(tag, name, days=args.days, now=args.now)

    print(f"=== FACTS BRIEF — {ctx['name']} ({tag}) ===")
    print(member_report.facts_for_model(ctx))
    print(f"\ncounts: battles={ctx['battles']['tally']['battles']} "
          f"badges={len(ctx['badges'])} cards={len(ctx['cards'])} "
          f"ranked={len(ctx['ranked'])} new_cards={len(ctx['game_stream']['new_cards'])}")
    if args.facts:
        return 0

    # Narrative + render come online in the next build step; guard so the context
    # stage is usable on its own.
    try:
        from agent.workflows import generate_member_report
        from runtime.member_report import render_member_report
    except Exception as exc:
        print(f"\n[render not wired yet: {exc}]")
        return 0

    narrative = generate_member_report(member_report.facts_for_model(ctx))
    subject, body = render_member_report(ctx, narrative)
    print(f"\n=== SUBJECT ===\n{subject}")
    if args.html:
        from agent.mail import email_render
        open(args.html, "w").write(email_render.text_to_html(body))
        print(f"\n[wrote HTML → {args.html}]")
    elif args.to:
        from agent.mail import outbound
        res = outbound.send(to=args.to, subject=subject, body=body)
        print(f"\n[sent to {res['to']} — emailId {res.get('emailId')}]")
    else:
        print(f"\n=== BODY (markdown) ===\n{body}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
