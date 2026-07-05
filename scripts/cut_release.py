"""Cut an Elixir release — the full ceremony, ported from Oliver's flow.

    ./venv/bin/python scripts/cut_release.py --version v5.1 --dry-run
    ./venv/bin/python scripts/cut_release.py --version v5.1

Flow (mirrors Oliver's /oliver release-notes "list" send, email → Discord):
 1. Gather git material since the latest v* tag (or --since/--days).
 2. Coin the release name (alliteration on a Clash Royale card).
 3. Generate the first-person notes (subject + three-section body).
 4. Write the release into the repo: prepend RELEASES.md, bump the
    RELEASE_VERSION/RELEASE_CODENAME defaults in agent/core.py, commit.
    (Oliver records his release in the events table; Elixir's record is
    RELEASES.md + the tag, and the running bot reports RELEASE_LABEL from
    the code defaults at its next restart.)
 5. Tag that commit with the version and publish the GitHub release
    (best-effort — never blocks the announcement).
 6. Post the announcement to Discord #announcements (chunked), carrying
    the GitHub release URL. No email — Discord only (Jamie, 2026-07-04).

--dry-run prints everything (name, subject, notes, tag plan, announcement
chunks) and touches nothing. --no-announce cuts the release silently.
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

load_dotenv()  # standalone script: ELIXIR_DB_PATH + CLAUDE_API_KEY from .env

from agent import release_notes as rn  # noqa: E402

CORE_PY = os.path.join(rn.REPO_ROOT, "agent", "core.py")


def _prepend_releases_md(*, version: str, name: str, body: str) -> None:
    """Prepend the new section to RELEASES.md in the file's own convention
    (## vX.Y — Name / **Date:** …)."""
    text = open(rn.RELEASES_MD).read()
    today = datetime.date.today().isoformat()
    header, _, rest = text.partition("---\n")
    section = f"## {version} — {name or 'Unnamed'}\n\n**Date:** {today}\n\n{body.strip()}\n\n"
    open(rn.RELEASES_MD, "w").write(f"{header}---\n\n{section}{rest.lstrip()}")


def _bump_core_defaults(*, version: str, name: str) -> None:
    """Point the RELEASE_VERSION/RELEASE_CODENAME code defaults at this
    release so the running bot's RELEASE_LABEL is right after its next
    restart (startup.py posts it at boot)."""
    src = open(CORE_PY).read()
    src = re.sub(r'RELEASE_VERSION = os\.getenv\("ELIXIR_RELEASE_VERSION", "[^"]*"\)',
                 f'RELEASE_VERSION = os.getenv("ELIXIR_RELEASE_VERSION", "{version}")', src)
    src = re.sub(r'RELEASE_CODENAME = os\.getenv\("ELIXIR_RELEASE_CODENAME", "[^"]*"\)',
                 f'RELEASE_CODENAME = os.getenv("ELIXIR_RELEASE_CODENAME", "{name}")', src)
    open(CORE_PY, "w").write(src)


def _run(args: list[str], *, timeout: int = 60) -> None:
    subprocess.run(args, cwd=rn.REPO_ROOT, check=True, capture_output=True,
                   text=True, timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help='the release version, e.g. "v5.1"')
    parser.add_argument("--days", type=int, default=None, help="scope: look back N days")
    parser.add_argument("--since", metavar="REF",
                        help="scope: changes since this ref (default: latest v* tag)")
    parser.add_argument("--name", help="skip coining and use this release name")
    parser.add_argument("--dry-run", action="store_true", help="print everything, touch nothing")
    parser.add_argument("--no-announce", action="store_true", help="cut without the Discord post")
    parser.add_argument("--announce-only", action="store_true",
                        help="retry ONLY the Discord announcement for an already-cut "
                             "release (e.g. after granting #announcements permission)")
    args = parser.parse_args()

    if not re.fullmatch(r"v\d+\.\d+(\.\d+)?", args.version):
        print(f"version {args.version!r} doesn't look like v5.1 — refusing")
        return 2

    # Retry path: the release is already cut (tag + RELEASES.md + GitHub
    # release exist); only the Discord post failed (locked #announcements, a
    # transient 403). Regenerate the announcement from the shipped notes and
    # post it — no re-cut, no new commit.
    if args.announce_only:
        return _announce_only(args.version)

    if rn._git(["tag", "-l", args.version]).strip() and not args.dry_run:
        print(f"tag {args.version} already exists — refusing")
        return 2
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
    print(f"Release name: {name or '(nameless — coin failed)'}")

    # Generate directly from the gathered material (release_notes_draft would
    # re-gather and re-coin; the name is already settled above).
    from agent.core import _create_chat_completion
    resp = _create_chat_completion(
        workflow="release_notes",
        messages=[{"role": "user", "content": rn.release_notes_prompt(material)}],
        temperature=0.7, max_tokens=8192, timeout=300,
    )
    out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    body = rn._extract_notes(out)
    subject = rn._extract_subject(out) or f"Under my hood: what changed ({material['window']})"

    title = f'{args.version} "{name}"' if name else args.version
    print(f"Subject: {subject}")
    print(f"Tag: {args.version}  ·  GitHub title: {title}\n")

    if args.dry_run:
        print("=" * 72)
        print(body)
        print("=" * 72)
        chunks = rn.announcement_messages(subject=subject, body=body,
                                          release_url="(github-release-url)",
                                          version=args.version, name=name)
        print(f"\n[dry-run] would post {len(chunks)} message(s) to #announcements; "
              f"would prepend RELEASES.md; would set core defaults to "
              f'{args.version} / "{name}"; would tag + gh release create.')
        return 0

    # 4. the release commit
    _prepend_releases_md(version=args.version, name=name, body=body)
    _bump_core_defaults(version=args.version, name=name)
    _run(["git", "add", "RELEASES.md", "agent/core.py"])
    _run(["git", "commit", "-m",
          f"release: {title}\n\nCut by scripts/cut_release.py ({material['window']})."])
    _run(["git", "push", "origin", "main"], timeout=120)
    head = rn.head_commit()
    print(f"Release commit: {head}")

    # 5. tag + GitHub release (best-effort)
    url = rn.create_github_release(version=args.version, name=name, commit=head, body=body)
    print(f"GitHub release: {url or '(failed — see log; announcement continues)'}")

    # 6. Discord announcement — BEST-EFFORT. The release is already cut (commit,
    # tag, GitHub release); a Discord failure (locked #announcements, a 403)
    # must NOT abort the script and leave a scary partial state. Warn, and tell
    # the operator how to retry once the channel permission is fixed.
    if args.no_announce:
        print("Skipping announcement (--no-announce).")
        return 0
    chunks = rn.announcement_messages(subject=subject, body=body, release_url=url,
                                      version=args.version, name=name)
    try:
        sent = rn.post_announcement(chunks)
        print(f"Announced in #announcements ({sent} message(s)).")
    except Exception as exc:
        print(f"\n⚠️  Release {args.version} IS cut (commit + tag + GitHub release) — "
              f"but the #announcements post failed: {exc}\n"
              f"Grant Elixir SEND permission in #announcements, then retry just the post:\n"
              f"    ./venv/bin/python scripts/cut_release.py --version {args.version} --announce-only")
    return 0


def _releases_section(version: str) -> str:
    """The version's section from RELEASES.md (its shipped notes)."""
    text = open(rn.RELEASES_MD).read()
    m = re.search(rf'(## {re.escape(version)}\b.*?)(?=\n## v|\Z)', text, re.S)
    return m.group(1).strip() if m else ""


def _announce_only(version: str) -> int:
    """Post (or re-post) the Discord announcement for an already-cut release."""
    entry = next((h for h in rn.release_history() if h.get("version") == version), None)
    if not entry:
        print(f"{version} not found in RELEASES.md — cut the release first.")
        return 2
    name = entry.get("name") or ""
    body = _releases_section(version)
    url = f"https://github.com/jthingelstad/elixir-bot/releases/tag/{version}"
    subject = f'{version} "{name}"' if name else version
    chunks = rn.announcement_messages(subject=subject, body=body, release_url=url,
                                      version=version, name=name)
    try:
        sent = rn.post_announcement(chunks)
        print(f"Announced {version} in #announcements ({sent} message(s)).")
        return 0
    except Exception as exc:
        print(f"Announcement still failing: {exc}\n"
              f"Confirm Elixir has SEND permission in #announcements.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
