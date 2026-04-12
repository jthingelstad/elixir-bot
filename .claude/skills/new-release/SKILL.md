---
name: new-release
description: Cut a new Elixir release — bump version constants, add RELEASES.md entry, and post a system signal announcement
---

# New Release

Use this skill when the user has shipped a meaningful new feature set and wants to cut a new versioned release of Elixir. This is the checklist that keeps the in-Discord "Release" label, the `RELEASES.md` history, and the `#announcements` system signal all in sync. Forgetting any one of these three leaves Elixir reporting the wrong version or silently skipping the announcement.

## What to do

The user will tell you the version number, the codename, and what shipped. If any of those are missing, ask before touching files.

Do all three steps. A release is not complete until all three land in one commit.

### 1. Bump the release constants

Edit `/Users/jamie/Projects/elixir-bot/agent/core.py` lines 32–33:

```python
RELEASE_VERSION = os.getenv("ELIXIR_RELEASE_VERSION", "vX.Y")
RELEASE_CODENAME = os.getenv("ELIXIR_RELEASE_CODENAME", "Codename")
```

These are the env-var defaults. `RELEASE_LABEL` on line 34 composes automatically — do not edit it. Everything downstream (system prompt, status reports, telemetry, tests) reads from these two constants, so this is the only code location that needs updating.

Note: tests in `tests/test_elixir_channels.py` (lines 564, 597, 2233) mock the label with hard-coded strings. If any of those tests fail after the bump, update the mocks to match — do not roll back the version.

### 2. Append a new entry to `RELEASES.md`

Add a new section at the top (just under the `---` on line 5) following the established pattern. Read the existing v4.3 and v4.2 entries first to match voice and structure. Each entry contains:

- `## vX.Y — Codename`
- `**Date:** YYYY-MM-DD` (today's date in the user's timezone)
- A 1–3 sentence prose intro describing what shipped and why it matters
- `### <Area>` subsections with `- ` bullet points for each concrete change
- Trailing `---` separator before the previous release

Do not touch `VERSIONS.md`. That file is reserved for major-era markers (v1.0, v2.0, v3.0, v4.0), not every minor bump. Only add to it if the user explicitly says this is a new era.

### 3. Add a system signal for `#announcements`

Append a new dict to `STARTUP_SYSTEM_SIGNALS` in `/Users/jamie/Projects/elixir-bot/runtime/system_signals.py` (before the closing `]`). Use the same structure as the most recent `capability_unlock` signals in that file.

```python
{
    "signal_key": "capability_<short_name>_v1",
    "signal_type": "capability_unlock",
    "payload": {
        "title": "Achievement Unlocked: <Title>",
        "message": "<1-2 sentence summary for LLM context>",
        "discord_content": "<Full Discord-ready markdown post>",
        "details": [
            "<Bullet point 1>",
            "<Bullet point 2>",
        ],
        "audience": "clan",
        "capability_area": "<snake_case_area>",
    },
},
```

Style notes for `discord_content`:
- Start with `**Achievement Unlocked: <Title>**` on its own line.
- 1–2 intro sentences, then bold section headers (`**How it works:**`, `**What's new:**`) with `- ` bullets.
- Under 2000 characters (Discord limit).
- Zero to two emoji total — do not spam.
- Sound like Elixir announcing a real upgrade, not a changelog.
- **Never use markdown tables** — Discord doesn't render them. Use bulleted lists with inline fields instead (e.g. `- **Name** — wins: 12 · fame: 2400`).

Signals only fire once per `signal_key`, so bump the `_v1` suffix if you are replacing an older signal for the same capability.

### 4. Validate

Run the tests:

```
venv/bin/python -m pytest tests/ -x -q
```

The system-signal test checks the new signal structurally and will catch schema drift. If any release-label mock tests fail, update the mocks (see note in step 1).

### 5. Report back

Summarize what was changed (version bump old → new, new RELEASES.md section title, new signal_key) and remind the user to commit. Do not commit unless the user asks.

## Arguments

$ARGUMENTS
