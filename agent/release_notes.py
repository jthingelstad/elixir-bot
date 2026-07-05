"""Release notes: Elixir tells the clan, in his own voice, what new capabilities
he's recently gained.

Ported from Oliver (rwbookclub.com agent/club/release_notes.py) at Jamie's
direction — same shape end to end: gather git source material, coin a release
NAME, prompt the model for a first-person announcement with a strict grounding
rule, extract <subject>/<notes> tags, cut the GitHub release. Two adaptations:
- No email. The announcement goes to Discord #announcements only, and it goes
  LAST so the post can carry the GitHub release URL (Oliver emails first
  because his email carries no link).
- The release name alliterates on a Clash Royale CARD instead of a book from
  the club's shelf ("Blazing Balloon" instead of "Quixotic Quicksilver") —
  the card catalog is Elixir's shelf. Used names come from RELEASES.md's
  `## vX.Y — Name` headers, the repo's release record since v4.1.

Scope is either a day window (--days) or everything since a commit-ish
(--since); the default is everything since the LATEST v* TAG — Elixir tags
every release (v4.6, v4.7, v4.8 …), so the tag itself is the baseline Oliver
keeps in his events table.

Build-time preview:
    python -m agent.release_notes --days 7          # print the draft
    python -m agent.release_notes                   # scope: since latest v-tag
(The full cut — RELEASES.md, version bump, tag, GitHub release, Discord
announcement — is scripts/cut_release.py.)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import urllib.request

log = logging.getLogger("elixir.release_notes")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASES_MD = os.path.join(REPO_ROOT, "RELEASES.md")

# A dense rework day can produce dozens of commits; cap the detailed list so
# the prompt stays bounded, and say so rather than dropping silently. (Carried
# from Oliver; the v5.1 epic will blow well past it, which the note handles.)
_COMMIT_CAP = 60

_SUBJECT_TAG = re.compile(r"<subject>(.*?)</subject>", re.S | re.I)
_NOTES_TAG = re.compile(r"<notes>(.*?)</notes>", re.S | re.I)
_RELEASE_HEADER = re.compile(r"^## (v[\d.]+) — (.+?)\s*$", re.M)

ANNOUNCEMENTS_CHANNEL_ID = "1474760975851982959"  # prompts/DISCORD.md #announcements


def _git(args: list[str]) -> str:
    """Read-only git output, '' on any failure (Oliver's publish.git_output)."""
    try:
        return subprocess.check_output(
            ["git", *args], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
        )
    except (subprocess.SubprocessError, OSError):
        return ""


def resolve_commit(ref: str) -> str | None:
    """Short hash for a commit-ish, or None (validates --since)."""
    out = _git(["rev-parse", "--verify", "--short", f"{ref}^{{commit}}"]).strip()
    return out or None


def head_commit() -> str | None:
    return resolve_commit("HEAD")


def latest_release_tag() -> str | None:
    """The newest v* tag by version sort — the default release baseline."""
    tags = [t for t in _git(["tag", "-l", "v*", "--sort=-v:refname"]).splitlines() if t.strip()]
    return tags[0] if tags else None


def release_history() -> list[dict]:
    """Every release in RELEASES.md, newest first: {version, name}. This file
    is Elixir's release record (Oliver keeps his in the events table)."""
    try:
        text = open(RELEASES_MD).read()
    except OSError:
        return []
    return [{"version": v, "name": n} for v, n in _RELEASE_HEADER.findall(text)]


def recent_changes(*, days: int | None = None, since_ref: str | None = None) -> dict:
    """Gather the git source material, scoped to a day window OR everything
    since a ref. Default: since the latest v* tag; last resort 7 days."""
    if not since_ref and days is None:
        since_ref = latest_release_tag()
    if since_ref:
        rev = [f"{since_ref}..HEAD"]
        info = _git(["log", "-1", "--date=short", "--pretty=format:%h %ad %s", since_ref]).strip()
        window = f"since {since_ref}" + (f" ({info.split(' ', 1)[1][:10]})" if info else "")
    else:
        days = days or 7
        rev = [f"--since={days} days ago"]
        window = f"the last {days} days"

    total = [ln for ln in _git(["log", *rev, "--oneline"]).splitlines() if ln]
    count = len(total)
    merges = _git(["log", *rev, "--merges", "--pretty=format:- %h %s"]).strip()
    commits = _git([
        "log", *rev, "-n", str(_COMMIT_CAP), "--no-merges", "--stat", "--date=short",
        "--pretty=format:%n### %h %ad %s%n%b",
    ]).strip()
    doc_lines = _git(["log", *rev, "--name-only", "--pretty=format:", "--", "*.md"]).splitlines()
    changed_docs = sorted({ln.strip() for ln in doc_lines if ln.strip().endswith(".md")})
    try:
        releases_head = "\n".join(open(RELEASES_MD).read().splitlines()[:60])
    except OSError:
        releases_head = ""

    return {
        "window": window,
        "days": days,
        "since_ref": since_ref,
        "count": count,
        "truncated": count > _COMMIT_CAP,
        "merges": merges,
        "commits": commits,
        "changed_docs": changed_docs,
        "releases_head": releases_head,
    }


def _card_names() -> list[str]:
    """The clan's shelf: every card in the synced catalog."""
    import db

    conn = db.get_connection()
    try:
        return [r["name"] for r in conn.execute(
            "SELECT name FROM card_catalog ORDER BY name").fetchall()]
    finally:
        conn.close()


def coin_release_name(material: dict) -> str:
    """Coin this release's name: a simple alliteration on ONE Clash Royale card
    ("Blazing Balloon"). A lightweight-model call — the name is a garnish —
    and failure-tolerant: any error returns "" and the release ships nameless
    rather than blocked. (Oliver's coin_release_name, shelf → card catalog.)"""
    try:
        from agent.core import _create_chat_completion

        cards = _card_names()
        if not cards:
            return ""
        used = [r["name"] for r in release_history() if r.get("name")]
        used_block = ("\n".join(f"- {n}" for n in used)
                      if used else "(none yet — this is the first named release)")
        user = (
            "Coin the name for this release of Elixir (POAP KINGS' Clash Royale clan agent). "
            "Rules:\n"
            "- Pick ONE card from the catalog below whose spirit fits this batch of changes.\n"
            "- The name is: one alliterative adjective + that card's distinctive word, card word "
            "LAST — for \"Balloon\" you might coin \"Blazing Balloon\". The adjective MUST start "
            "with the same letter/sound as the card word. Two or three words total; never append "
            "extra words after the card word.\n"
            "- Never reuse a previously used name OR its anchor card.\n"
            "- Output ONLY the name on a single line. No quotes, no explanation.\n\n"
            "--- What shipped in this batch ---\n"
            f"{material.get('merges') or (material.get('commits') or '')[:2000] or '(small batch)'}\n\n"
            "--- Previously used release names ---\n"
            f"{used_block}\n\n"
            "--- The card catalog (pick your anchor card from these) ---\n"
            + "\n".join(f"- {c}" for c in cards)
        )
        resp = _create_chat_completion(
            workflow="lightweight",
            system="You name software releases for a Clash Royale clan's agent. "
                   "You answer with the name only.",
            messages=[{"role": "user", "content": user}],
            temperature=0.7,
            max_tokens=200,
        )
        name = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        name = name.strip().strip('"').strip("'")
        if not name or "\n" in name or len(name) > 60:
            return ""
        return name
    except Exception:
        log.exception("release-name coin failed; shipping nameless")
        return ""


def release_notes_prompt(material: dict) -> str:
    """Oliver's prompt, re-voiced for Elixir and the clan: same three-section
    structure, same grounding rule, same open/close contract. Discord renders
    markdown, so the format guidance drops the HTML-email framing."""
    window = material["window"]
    trunc = (
        f"\n(NOTE: {material['count']} commits landed in this window; only the {_COMMIT_CAP} "
        "most recent are shown in full below. Say so if it matters.)"
        if material["truncated"] else ""
    )
    docs = "\n".join(f"- {d}" for d in material["changed_docs"]) or "(no docs changed)"
    name = material.get("release_name") or ""
    naming = (
        f"RELEASE NAME: this release has been christened \"{name}\" — every release of your "
        "software is named with an alliteration on a Clash Royale card. Your OPEN framing "
        "sentence must introduce the release by this name; a short clause on why that card fits "
        "this batch is welcome. The subject MAY carry the name too, if it lands naturally.\n\n"
        if name else ""
    )
    return (
        "Write a short announcement to the POAP KINGS clan Discord describing the new "
        f"capabilities YOU — Elixir — have gained. This batch covers {window}. This is you, in "
        "first person, telling the clan what you can now do and what changed under the hood.\n\n"
        "VOICE: first person throughout (\"I can now…\", \"I rebuilt…\", \"I learned…\"). "
        "Specific and honest — name the actual mechanism, don't be hand-wavey — but pitched for "
        "clan members, not engineers: lead with what they'll notice, and where the internals are "
        "genuinely interesting, share them with a sentence of WHY the change is good — what it "
        "improves, what it prevents, what it makes possible.\n\n"
        "GROUNDING (important): describe ONLY changes that appear in the material below. Do not "
        "invent features, numbers, or capabilities. If something is unclear, leave it out. Where "
        "a change is internal plumbing, it's fine — explain it honestly and make it interesting.\n\n"
        "OPEN: before the first section (no header), write ONE short framing sentence so a reader "
        f"knows immediately what this is and the window it covers. Don't dive straight into the "
        "story.\n\n"
        f"{naming}"
        "STRUCTURE — exactly three '## ' sections, in this order:\n\n"
        "## The story\n"
        "A short narrative (2-4 sentences, prose) of what's genuinely interesting in this batch — "
        "the throughline, what you were working toward, why it matters to the clan.\n\n"
        "## Features\n"
        "A bulleted list of the things members should actually notice and use — the changes that "
        "touch their experience. Lead each bullet with the capability, then a sentence on why "
        "it's good. Keep it to what a member cares about.\n\n"
        "## Release Notes\n"
        "A terse changelog — MANY short, specific entries, one concrete fact per bullet (what "
        "changed, named precisely), NOT paragraphs. Same factual texture as Features but more of "
        "them and lower-level: prefer a dozen one-line facts over three explanations. Each "
        "bullet is a fact, not a story; no prose lead-in, just the list.\n\n"
        "CLOSE: after the last section (no header), end with one short, warm sign-off sentence "
        "in your voice — wrap up and/or point them to #ask-elixir. Do NOT add your name or a "
        "signature block.\n\n"
        "FORMAT: this renders in Discord and on GitHub, so use markdown — a '## ' header for "
        "each section, bulleted lists, *italics* for feature/file names, **bold** sparingly on a "
        "key phrase. Blank line between paragraphs and after each header. No horizontal rules.\n\n"
        "OUTPUT: first write a single-line subject — fun, fitting, and a little bit clever for "
        "this audience — between <subject> and </subject>. Then write the ENTIRE announcement "
        "between <notes> and </notes> tags, with NOTHING outside the two tag pairs.\n\n"
        f"=== SOURCE MATERIAL ({window}) ==={trunc}\n\n"
        "--- Shipped features (merge commits) ---\n"
        f"{material['merges'] or '(none — this repo ships directly to main)'}\n\n"
        "--- Commits in detail (subject, body, files changed) ---\n"
        f"{material['commits'] or '(none)'}\n\n"
        "--- Docs changed in this window ---\n"
        f"{docs}\n\n"
        "--- RELEASES.md (recent head — the running release record, for context) ---\n"
        f"{material['releases_head'] or '(unavailable)'}\n"
    )


def _extract_subject(text: str) -> str:
    m = _SUBJECT_TAG.search(text)
    if m:
        return " ".join(m.group(1).split()).strip()
    opened = re.search(r"<subject>", text, re.I)
    if opened:
        rest = text[opened.end():].strip()
        first = rest.splitlines()[0] if rest else ""
        return re.sub(r"</?subject\s*>", "", first, flags=re.I).strip()
    return ""


def _extract_notes(text: str) -> str:
    m = _NOTES_TAG.search(text)
    if m:
        return m.group(1).strip()
    opened = re.search(r"<notes>", text, re.I)
    if opened:
        return re.sub(r"</notes\s*>", "", text[opened.end():], flags=re.I).strip()
    return text.strip()


def release_notes_draft(*, days: int | None = None, since_ref: str | None = None) -> dict | None:
    """Build the announcement. Returns {subject, body, window, release_name},
    or None if there were no changes in the window."""
    material = recent_changes(days=days, since_ref=since_ref)
    if material["count"] == 0:
        return None
    material["release_name"] = coin_release_name(material)

    from agent.core import _create_chat_completion

    resp = _create_chat_completion(
        workflow="release_notes",
        messages=[{"role": "user", "content": release_notes_prompt(material)}],
        temperature=0.7,
        max_tokens=8192,
        timeout=300,
    )
    out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    body = _extract_notes(out)
    subject = _extract_subject(out) or f"Under my hood: what changed ({material['window']})"
    return {"subject": subject, "body": body, "window": material["window"],
            "release_name": material["release_name"]}


def create_github_release(*, version: str, name: str, commit: str, body: str) -> str | None:
    """Tag `commit` and publish the GitHub release — the permanent code
    reference for what shipped. Tag = the version (repo convention: v4.8 …);
    title carries the christened name. Best-effort by contract (Oliver's
    rule): the announcement must never be blocked by GitHub, so failures log
    and return None. An existing tag/release is reused."""
    tag = version
    title = f'{version} "{name}"' if name else version
    try:
        if not _git(["tag", "-l", tag]).strip():
            subprocess.run(["git", "tag", "-a", tag, commit, "-m", f"{title} — Elixir release"],
                           cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=15)
            subprocess.run(["git", "push", "origin", tag],
                           cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=60)
        view = subprocess.run(["gh", "release", "view", tag, "--json", "url", "-q", ".url"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        if view.returncode == 0 and view.stdout.strip():
            return view.stdout.strip()
        made = subprocess.run(["gh", "release", "create", tag, "--title", title,
                               "--notes-file", "-"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
                              input=body)
        if made.returncode != 0:
            raise RuntimeError((made.stderr or made.stdout or "")[-500:])
        return made.stdout.strip() or None
    except Exception:
        log.exception("GitHub release for %r failed", tag)
        return None


# ------------------------------------------------------ Discord announcement

def announcement_messages(*, subject: str, body: str, release_url: str | None,
                          version: str, name: str) -> list[str]:
    """The #announcements post: the full notes, chunked to Discord's limit,
    opening with the release title line and closing with the GitHub link."""
    from runtime.helpers import DISCORD_CHUNK_SIZE, _chunk_for_discord

    title = f'**{version} "{name}"**' if name else f"**{version}**"
    text = f"{title} — {subject}\n\n{body}"
    if release_url:
        text += f"\n\n-# Full release on GitHub: {release_url}"
    return _chunk_for_discord(text, size=DISCORD_CHUNK_SIZE)


def post_announcement(messages: list[str]) -> int:
    """Send the announcement to #announcements via the Discord REST API with
    the bot token — no runtime restart needed; the live bot is untouched."""
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")
    sent = 0
    for content in messages:
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{ANNOUNCEMENTS_CHANNEL_ID}/messages",
            data=json.dumps({"content": content}).encode(),
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        sent += 1
    return sent


def _main() -> None:
    from dotenv import load_dotenv

    load_dotenv()  # standalone preview: env from .env, like the runtime does
    parser = argparse.ArgumentParser(description="Preview Elixir's release notes draft.")
    parser.add_argument("--days", type=int, default=None, help="look back this many days")
    parser.add_argument("--since", metavar="REF",
                        help="scope to changes since this commit-ish (default: latest v* tag)")
    args = parser.parse_args()

    draft = release_notes_draft(days=args.days, since_ref=args.since)
    if draft is None:
        print("No changes in the window — nothing to announce.")
        return
    if draft.get("release_name"):
        print(f"Release name: {draft['release_name']}")
    print(f"Subject: {draft['subject']}\n")
    print(draft["body"])


if __name__ == "__main__":
    _main()
