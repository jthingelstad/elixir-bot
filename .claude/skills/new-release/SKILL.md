---
name: new-release
description: Cut an Elixir named release with the canonical release script, verification, and live announcement audit
---

# New Release

Elixir releases use a coined Clash Royale name, date, and build hash — not
SemVer. `scripts/cut_release.py` is the canonical ceremony. It gathers changes,
coins or accepts a name, generates detailed/Discord/clan-chat tiers, updates
`RELEASES.md` and `agent/core.py`, commits, pushes, tags, creates the GitHub
release, records durable memory, emails the clan, and posts the announcement.

Do not hand-edit version constants or add a startup system signal for an
ordinary release. The old `RELEASE_VERSION`/`capability_unlock` checklist is
retired.

## Before cutting

1. Read `AGENTS.md` and verify the worktree and branch.
2. Confirm the intended change window (`--since REF` or `--days N`).
3. Run the standard tests and confidence gates appropriate to the release.
4. The non-dry-run script requires a clean worktree.

## Preview

Always inspect the generated three-tier copy before publishing:

```bash
./venv/bin/python scripts/cut_release.py --dry-run --since <ref>
```

The user may supply the coined name:

```bash
./venv/bin/python scripts/cut_release.py --dry-run --since <ref> --name "Blazing Balloon"
```

If no name is supplied, the script coins one. Report it for review before the
non-dry-run cut when the user has not already authorized autonomous publishing.

## Cut

```bash
./venv/bin/python scripts/cut_release.py --since <ref> --name "Blazing Balloon"
```

Use `--no-email` or `--no-announce` only when explicitly requested or when that
surface is unavailable. To retry only a failed Discord announcement after the
release itself succeeded:

```bash
./venv/bin/python scripts/cut_release.py --announce-only blazing-balloon
```

## Verify

After the script returns:

1. Confirm the release commit and named tag point at the expected build.
2. Confirm the GitHub Release URL.
3. Confirm `RELEASE_CODENAME` and `RELEASE_STAMP` in `agent/core.py`.
4. Confirm the detailed entry is at the top of `RELEASES.md`.
5. Check the script’s email, Discord announcement, durable-memory, and clan-chat
   results individually; these best-effort surfaces may fail after the release
   commit/tag already succeeded.
6. Restart production when the release changes runtime code, then verify status,
   fresh startup logs, and the running build hash.

Never rerun a full cut merely because one best-effort notification failed.
Report the release label, commit, tag, GitHub URL, delivery results, and live
build verification.

## Arguments

$ARGUMENTS
