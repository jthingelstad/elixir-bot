"""Cut an Elixir release — the full ceremony (no version number).

    ./venv/bin/python scripts/cut_release.py --dry-run
    ./venv/bin/python scripts/cut_release.py

A release is identified by its coined NAME + DATE + build hash — no SemVer. The
git tag is the ref-safe name-slug ("Blazing Balloon" → tag `blazing-balloon`);
the human label is "Blazing Balloon (2026-07-08)".

Flow:
 1. Gather git material since the latest release tag (or --since/--days).
 2. Coin the release name (alliteration on a Clash Royale card) — settles the tag.
 3. Generate THREE tiers in one model call: detailed (email + GitHub +
    RELEASES.md), announcement (Discord #announcements), clanchat (in-game blurb).
 4. Write the release: prepend RELEASES.md, bump RELEASE_CODENAME/RELEASE_STAMP in
    agent/core.py, commit, push.
 5. Tag the commit with the name-slug and publish the GitHub release (best-effort).
 6. Email the detailed tier from elixir@poapkings.com (best-effort).
 7. Post the announcement tier to #announcements (best-effort).
 8. Emit the clanchat tier (marker line) — /elixir release posts it as a
    leader-action card in #leader-actions.

--dry-run prints every tier and touches nothing. --no-announce / --no-email skip
those sends. --announce-only re-posts the Discord announcement for an already-cut
release (keyed by its tag slug).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # standalone script: ELIXIR_DB_PATH + CLAUDE_API_KEY + FASTMAIL_JMAP_TOKEN from .env

from agent import release_notes as rn  # noqa: E402

CORE_PY = os.path.join(rn.REPO_ROOT, "agent", "core.py")
EMAIL_ADDRESS = os.getenv("ELIXIR_EMAIL_ADDRESS", "elixir@poapkings.com")
# The slash command greps stdout for this marker to post the clan-chat card.
CLANCHAT_MARKER = "::RELEASE_CLANCHAT::"


def _label(name: str, date: str) -> str:
    return f"{name} ({date})" if name else f"Release ({date})"


def _prepend_releases_md(*, label: str, date: str, body: str) -> None:
    """Prepend the new section to RELEASES.md: `## {label}` + a machine-readable
    `**Date:**` line + the detailed body."""
    text = open(rn.RELEASES_MD).read()
    header, _, rest = text.partition("---\n")
    section = f"## {label}\n\n**Date:** {date}\n\n{body.strip()}\n\n"
    open(rn.RELEASES_MD, "w").write(f"{header}---\n\n{section}{rest.lstrip()}")


def _bump_core_defaults(*, name: str, date: str) -> None:
    """Point the RELEASE_CODENAME/RELEASE_STAMP code defaults at this release so the
    running bot's RELEASE_LABEL ('Name (date)') is right after its next restart."""
    src = open(CORE_PY).read()
    src = re.sub(r'RELEASE_CODENAME = os\.getenv\("ELIXIR_RELEASE_CODENAME", "[^"]*"\)',
                 f'RELEASE_CODENAME = os.getenv("ELIXIR_RELEASE_CODENAME", "{name}")', src)
    src = re.sub(r'RELEASE_STAMP = os\.getenv\("ELIXIR_RELEASE_STAMP", "[^"]*"\)',
                 f'RELEASE_STAMP = os.getenv("ELIXIR_RELEASE_STAMP", "{date}")', src)
    open(CORE_PY, "w").write(src)


def _run(args: list[str], *, timeout: int = 60) -> None:
    subprocess.run(args, cwd=rn.REPO_ROOT, check=True, capture_output=True,
                   text=True, timeout=timeout)


def _derive_tag(name: str, *, head: str | None, date: str) -> str:
    """The ref-safe git tag: the name-slug, or a dated fallback when nameless."""
    slug = rn.slugify_release(name)
    if slug:
        return slug
    return f"release-{date}-{head or 'head'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=None, help="scope: look back N days")
    parser.add_argument("--since", metavar="REF",
                        help="scope: changes since this ref (default: latest release tag)")
    parser.add_argument("--name", help="skip coining and use this release name")
    parser.add_argument("--to", default=None,
                        help="override: email ONLY this address (testing). Default broadcasts "
                             "the detailed tier to every clan member with a verified email.")
    parser.add_argument("--dry-run", action="store_true", help="print everything, touch nothing")
    parser.add_argument("--no-announce", action="store_true", help="cut without the Discord post")
    parser.add_argument("--no-email", action="store_true", help="cut without the email")
    parser.add_argument("--announce-only", metavar="TAG",
                        help="retry ONLY the Discord announcement for an already-cut release "
                             "(by its tag slug, e.g. blazing-balloon)")
    args = parser.parse_args()

    if args.announce_only:
        return _announce_only(args.announce_only)

    dirty = rn._git(["status", "--porcelain"]).strip()
    if dirty and not args.dry_run:
        print("working tree is not clean — commit or stash first:\n" + dirty)
        return 2

    material = rn.recent_changes(days=args.days, since_ref=args.since)
    if material["count"] == 0:
        print("No changes in the window — nothing to release.")
        return 0
    print(f"Scope: {material['window']} ({material['count']} commits)")

    name = args.name if args.name is not None else rn.coin_release_name(material)
    material["release_name"] = name
    date = datetime.date.today().isoformat()
    tag = _derive_tag(name, head=rn.head_commit(), date=date)
    label = _label(name, date)
    print(f"Release: {label}  ·  tag {tag}")

    if rn._git(["tag", "-l", tag]).strip() and not args.dry_run:
        print(f"tag {tag} already exists — refusing")
        return 2

    # Generate the three tiers in one call (release_notes_draft re-gathers and
    # re-coins; the name is already settled, so build from this material directly).
    from agent.core import _create_chat_completion
    resp = _create_chat_completion(
        workflow="release_notes",
        messages=[{"role": "user", "content": rn.release_notes_prompt(material)}],
        temperature=0.7, max_tokens=8192, timeout=300,
    )
    out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    detailed = rn._extract_notes(out)
    announcement = rn._extract_tag(out, "announcement") or detailed
    clanchat = " ".join((rn._extract_tag(out, "clanchat") or "").split())
    subject = rn._extract_subject(out) or f"Under my hood: what changed ({material['window']})"
    if name and name.lower() not in subject.lower():
        subject = f"{name} — {subject}"
    print(f"Subject: {subject}")

    if args.dry_run:
        for tier, text in (("DETAILED (email + GitHub)", detailed),
                           ("ANNOUNCEMENT (#announcements)", announcement),
                           ("CLANCHAT (#leader-actions card)", clanchat or "(none)")):
            print("\n" + "=" * 72 + f"\n{tier}\n" + "=" * 72 + f"\n{text}")
        try:
            import db
            n_recips = 1 if args.to else len(db.list_member_emails())
        except Exception:
            n_recips = "?"
        email_to = args.to if args.to else f"{n_recips} member(s) with a verified email"
        print(f"\n[dry-run] would prepend RELEASES.md (## {label}); set core defaults to "
              f'"{name}" / {date}; tag {tag} + gh release create; email to {email_to}; '
              f"post the announcement; surface the clan-chat card.")
        return 0

    # 4. the release commit
    _prepend_releases_md(label=label, date=date, body=detailed)
    _bump_core_defaults(name=name, date=date)
    _run(["git", "add", "RELEASES.md", "agent/core.py"])
    _run(["git", "commit", "-m",
          f"release: {label}\n\nCut by scripts/cut_release.py ({material['window']})."])
    _run(["git", "push", "origin", "main"], timeout=120)
    head = rn.head_commit()
    print(f"Release commit: {head}")

    # 5. tag + GitHub release (best-effort)
    url = rn.create_github_release(name=name, date=date, tag=tag, commit=head, body=detailed)
    print(f"GitHub release: {url or '(failed — see log; the cut continues)'}")

    # 6. Email the detailed tier to clan members with a verified email (BCC so
    # nobody sees anyone else's address). Best-effort — never blocks the rest.
    if not args.no_email:
        try:
            import db
            from agent.mail import outbound
            if not outbound.enabled():
                print("Email skipped: FASTMAIL_JMAP_TOKEN not configured.")
            else:
                recipients = [args.to] if args.to else [m["email"] for m in db.list_member_emails()]
                if not recipients:
                    print("Email skipped: no clan members have a verified email on file.")
                else:
                    outbound.send(to=EMAIL_ADDRESS, bcc=recipients, subject=subject, body=detailed)
                    where = args.to if args.to else f"{len(recipients)} member(s) (bcc)"
                    print(f"Emailed detailed notes to {where}.")
        except Exception as exc:
            print(f"⚠️  Email failed (release still cut): {exc}")

    # 7. Discord announcement — BEST-EFFORT (a locked channel / 403 must not abort).
    if not args.no_announce:
        chunks = rn.announcement_messages(announcement=announcement, release_url=url,
                                          name=name, date=date)
        try:
            sent = rn.post_announcement(chunks)
            print(f"Announced in #announcements ({sent} message(s)).")
        except Exception as exc:
            print(f"\n⚠️  Release {tag} IS cut (commit + tag + GitHub release) — "
                  f"but the #announcements post failed: {exc}\n"
                  f"Grant Elixir SEND permission in #announcements, then retry just the post:\n"
                  f"    ./venv/bin/python scripts/cut_release.py --announce-only {tag}")

    # 8. Clan-chat tier — hand the blurb to the caller (the slash command posts the
    # #leader-actions card; a bare CLI cut just prints it for a human to paste).
    if clanchat:
        print(f"{CLANCHAT_MARKER}{tag}{CLANCHAT_MARKER}{clanchat}")
    return 0


def _releases_section(label_or_tag: str) -> str:
    """The release's detailed section from RELEASES.md, matched by header label."""
    text = open(rn.RELEASES_MD).read()
    m = re.search(rf'(## {re.escape(label_or_tag)}\b.*?)(?=\n## |\Z)', text, re.S)
    return m.group(1).strip() if m else ""


def _announce_only(tag: str) -> int:
    """Re-post the Discord announcement for an already-cut release, found by tag slug."""
    entry = next((h for h in rn.release_history()
                  if rn.slugify_release(h.get("name") or "") == tag), None)
    if not entry:
        print(f"No release with tag {tag} found in RELEASES.md — cut it first.")
        return 2
    name = entry.get("name") or ""
    date = entry.get("date") or ""
    body = _releases_section(_label(name, date)) or _releases_section(name)
    url = f"https://github.com/jthingelstad/elixir-bot/releases/tag/{tag}"
    chunks = rn.announcement_messages(announcement=body, release_url=url, name=name, date=date)
    try:
        sent = rn.post_announcement(chunks)
        print(f"Announced {tag} in #announcements ({sent} message(s)).")
        return 0
    except Exception as exc:
        print(f"Announcement still failing: {exc}\n"
              f"Confirm Elixir has SEND permission in #announcements.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
