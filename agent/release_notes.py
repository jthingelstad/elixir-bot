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
import shutil
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
# Release headers in RELEASES.md come in two shapes: the legacy versioned
# `## v5.1 — Consolidated Collector` and the current, version-free
# `## Blazing Balloon (2026-07-08)`. Match ONLY those two — never the
# `## The story` / `## Features` section headers inside a release body.
_RELEASE_HEADER = re.compile(
    r"^## (?:(v[\d.]+) — (.+?)|(.+?) \((\d{4}-\d{2}-\d{2})\))\s*$", re.M)


def slugify_release(name: str) -> str:
    """Ref-safe git-tag slug from a coined name: 'Blazing Balloon' → 'blazing-balloon'.
    Returns '' for an empty/None name so the caller can fall back to a dated tag."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")

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
    """The most recently created tag — the default release baseline. Sorted by
    creation date, not version, so it bridges the scheme change cleanly: it
    returns the last `v*` tag today and the newest name-slug tag after the first
    version-free cut (name-slug tags have no version ordering to sort on)."""
    tags = [t for t in _git(["tag", "--sort=-creatordate"]).splitlines() if t.strip()]
    return tags[0] if tags else None


def release_history() -> list[dict]:
    """Every release in RELEASES.md, newest first. Each entry always carries a
    normalized `name`; legacy entries also carry `version`, current entries carry
    `date`. This file is Elixir's release record (Oliver keeps his in the events
    table). Both header shapes are parsed so the coin-dedup below still sees the
    full name history across the scheme change."""
    try:
        text = open(RELEASES_MD).read()
    except OSError:
        return []
    out: list[dict] = []
    for m in _RELEASE_HEADER.finditer(text):
        if m.group(1):  # legacy `## v5.1 — Name`
            out.append({"version": m.group(1), "name": m.group(2).strip(), "date": None})
        else:           # current `## Name (YYYY-MM-DD)`
            out.append({"version": None, "name": m.group(3).strip(), "date": m.group(4)})
    return out


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
        "FORMAT: the detailed version renders on GitHub and in email, so use markdown — a '## ' "
        "header for each section, bulleted lists, *italics* for feature/file names, **bold** "
        "sparingly. Blank line between paragraphs and after each header. No horizontal rules.\n\n"
        "THREE VERSIONS — write the SAME announcement at three lengths, all grounded in the SAME "
        "material below, longest to shortest:\n"
        "1. DETAILED — the full three-section piece described above (the story + features + "
        "release notes, with the OPEN framing and CLOSE sign-off). This is the email and the "
        "GitHub release.\n"
        "2. ANNOUNCEMENT — a Discord #announcements post: the OPEN framing sentence, a tight 2-3 "
        "sentence story, and 3-5 of the biggest member-facing features as short bullets. NO full "
        "changelog. Lead with what the clan will notice. Markdown is fine; keep it well under "
        "1500 characters. Still first person, still your voice.\n"
        "3. CLANCHAT — ONE or two sentences for the in-game clan chat, the way a leader would "
        "actually say it out loud. Name the release and the single most exciting thing. PLAIN "
        "TEXT ONLY: no markdown, no links, no headers, no emoji shortcodes; under 180 characters "
        "so it fits one clan-chat line. Do NOT sign it.\n\n"
        "OUTPUT: emit EXACTLY these four tag pairs and NOTHING outside them — a single-line "
        "subject (fun, fitting, a little clever) between <subject></subject>; the detailed "
        "version between <detailed></detailed>; the medium version between "
        "<announcement></announcement>; the short version between <clanchat></clanchat>.\n\n"
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


def _extract_tag(text: str, name: str) -> str:
    """Content between <name>...</name> (case-insensitive, dot-all). Tolerates a
    missing close tag by taking everything after the open tag."""
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    opened = re.search(rf"<{name}>", text, re.I)
    if opened:
        return re.sub(rf"</{name}\s*>", "", text[opened.end():], flags=re.I).strip()
    return ""


def _extract_notes(text: str) -> str:
    """The detailed tier — the email/GitHub body. Accepts the current <detailed>
    tag or the legacy <notes> tag, and falls back to the whole text."""
    return _extract_tag(text, "detailed") or _extract_tag(text, "notes") or text.strip()


def release_notes_draft(*, days: int | None = None, since_ref: str | None = None) -> dict | None:
    """Build the three-tier announcement in ONE model call. Returns
    {subject, body, announcement, clanchat, window, release_name}, or None if
    there were no changes in the window. `body` is the detailed tier
    (email/GitHub); `announcement` is the Discord #announcements version;
    `clanchat` is the short in-game-clan-chat blurb."""
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
    return {
        "subject": _extract_subject(out) or f"Under my hood: what changed ({material['window']})",
        "body": _extract_notes(out),
        "announcement": _extract_tag(out, "announcement"),
        "clanchat": _extract_tag(out, "clanchat"),
        "window": material["window"],
        "release_name": material["release_name"],
    }


def _gh_bin() -> str | None:
    """Absolute path to the `gh` CLI. shutil.which first, then the common Homebrew
    locations — the bot runs under launchd with a minimal PATH that excludes
    /opt/homebrew/bin, so a bare 'gh' raises FileNotFoundError (git resolves via
    Apple's /usr/bin/git, but gh is Homebrew-only)."""
    found = shutil.which("gh")
    if found:
        return found
    return next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None)


def create_github_release(*, name: str, date: str, tag: str, commit: str, body: str) -> str | None:
    """Tag `commit` and publish the GitHub release — the permanent code
    reference for what shipped. `tag` is the ref-safe name-slug (e.g.
    'blazing-balloon'); the title carries the human label 'Name (date)'.
    Best-effort by contract (Oliver's rule): the announcement must never be
    blocked by GitHub, so failures log and return None. An existing tag/release
    is reused."""
    title = f"{name} ({date})" if name else f"Release ({date})"
    try:
        if not _git(["tag", "-l", tag]).strip():
            subprocess.run(["git", "tag", "-a", tag, commit, "-m", f"{title} — Elixir release"],
                           cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=15)
            subprocess.run(["git", "push", "origin", tag],
                           cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=60)
        gh = _gh_bin()
        if not gh:
            log.warning("gh CLI not found; tag %s pushed but no GitHub release created", tag)
            return None
        view = subprocess.run([gh, "release", "view", tag, "--json", "url", "-q", ".url"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        if view.returncode == 0 and view.stdout.strip():
            return view.stdout.strip()
        made = subprocess.run([gh, "release", "create", tag, "--title", title,
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

def announcement_messages(*, announcement: str, release_url: str | None,
                          name: str, date: str) -> list[str]:
    """The #announcements post: the MEDIUM tier, chunked to Discord's limit,
    opening with the 'Name (date)' title line and closing with the GitHub link."""
    from runtime.helpers import DISCORD_CHUNK_SIZE, _chunk_for_discord

    title = f"**{name} ({date})**" if name else f"**Release ({date})**"
    text = f"{title}\n\n{(announcement or '').strip()}"
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
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                # Discord REQUIRES a bot User-Agent; without it Cloudflare (in
                # front of the API) blocks urllib's default UA with a 1010/403
                # (live 2026-07-05: the first real release never announced).
                "User-Agent": "DiscordBot (https://github.com/jthingelstad/elixir-bot, 5.1)",
            },
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
