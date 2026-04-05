---
name: new-system-signal
description: Create a new system signal announcement for the clan in Elixir
---

# New System Signal

Create a new system signal announcement for Elixir. These are one-time announcements posted to #announcements on the next bot deploy to tell the clan about new features or changes.

## What to do

The user will describe the feature or change to announce. You need to:

1. **Read the existing signals** in `runtime/system_signals.py` to match the established style and structure.

2. **Append a new signal dict** to the `STARTUP_SYSTEM_SIGNALS` list at the end (before the closing `]`).

3. **Follow this exact structure:**

```python
{
    "signal_key": "<capability_or_feature>_<short_name>_v1",
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

### Field guide

| Field | Purpose |
|-------|---------|
| `signal_key` | Unique key. Convention: `capability_<name>_v1` or `feature_<name>_v1`. Bump version if replacing an older signal. |
| `signal_type` | Always `"capability_unlock"` for feature announcements. |
| `title` | Starts with `"Achievement Unlocked: "` followed by a short descriptive title. |
| `message` | 1-2 sentences summarizing the feature. Used as LLM context if the agent generates the post (when `discord_content` is absent). |
| `discord_content` | **Pre-authored Discord post.** When present, this is posted verbatim instead of LLM-generating it. Use Discord markdown (`**bold**`, `\n\n` for paragraphs). Start with the bold title line. Can include `:emoji_name:` shortcodes for custom server emoji. |
| `details` | List of bullet-point strings. Used as LLM context for generation. Keep each under ~150 chars. |
| `audience` | `"clan"` for public #announcements, `"leadership"` for #leader-lounge only. |
| `capability_area` | Snake_case identifier grouping related signals. |

### Style guidance for `discord_content`

- Start with `**Achievement Unlocked: <Title>**` on its own line, optionally with a `:elixir:` emoji.
- Follow with 1-2 intro sentences.
- Use bold section headers (`**How it works:**`, `**What's new:**`, etc.) to organize.
- Use `- ` bullet points under each section.
- End with a closing line that feels forward-looking or celebratory.
- Total length should be under 2000 characters (Discord limit).
- Avoid overusing emoji — zero to two total is the sweet spot.
- Sound like a proud clan system announcing a real upgrade, not a generic changelog.

4. **Verify the signal is valid** by running: `venv/bin/python -m pytest tests/ -x -q`

The test in `test_elixir_channels.py` dynamically checks against `STARTUP_SYSTEM_SIGNALS` so it should pass automatically without needing test updates.

## Arguments

$ARGUMENTS
