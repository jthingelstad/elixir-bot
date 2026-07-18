# scripts/

Operational utilities and eval harnesses for the Elixir bot.

Run everything from the repo root through the locked uv environment:

```bash
uv sync --locked
uv run --locked python scripts/<name>.py [args]
```

## Operations

### `admin.sh`
Service control for the launchd agent (`com.poapkings.elixir`).

```bash
scripts/admin.sh install    # write ~/Library/LaunchAgents/com.poapkings.elixir.plist
scripts/admin.sh start      # launchctl bootstrap
scripts/admin.sh stop       # launchctl bootout
scripts/admin.sh restart    # backup → stop → start
scripts/admin.sh status
scripts/admin.sh upgrade    # stop → git pull --ff-only → uv sync --locked → start
scripts/admin.sh backup     # invokes backup_db.py
```

`restart` creates a live SQLite backup before stopping the bot and aborts if the
backup fails.

### `backup_db.py`
Safe online SQLite backup (uses `sqlite3.Connection.backup()` — no need to stop
the bot) plus tiered retention pruning. Also imported by the db-maintenance
job.

```bash
uv run --locked python scripts/backup_db.py
```

- Source: the single operational database (engine + durable memory)
- Output: `~/elixir-backups/elixir-v51-YYYY-MM-DD-HHMMSS.db.gz` (gzip level 6)
- Integrity-checks the snapshot before compressing
- Retention: keep-all ≤28d · monthly 29–90d · quarterly 91–365d · delete >365d

Override via env:
- `ELIXIR_DB_PATH` — source database (default: `<repo>/elixir-v51.db`)
- `ELIXIR_BACKUP_DIR` — destination dir (default: `~/elixir-backups`)

### `elixir_state.py`
Read-only inspection of Elixir's event streams, awareness activity, war state,
and decision cases.

```bash
uv run --locked python scripts/elixir_state.py summary
uv run --locked python scripts/elixir_state.py events --days 28 --scope leadership
uv run --locked python scripts/elixir_state.py awareness --limit 25
uv run --locked python scripts/elixir_state.py war --json
uv run --locked python scripts/elixir_state.py cases --status due
```

Use this when you need to answer "what is Elixir monitoring?", "what
recommendations are open?", or "what did awareness recently post or skip?"
without reading raw Discord history.

### `clean.py`
Remove local cache/build cruft.

```bash
uv run --locked python scripts/clean.py           # removes __pycache__, .pytest_cache, .mypy_cache, .ruff_cache
uv run --locked python scripts/clean.py --db      # also removes elixir.db and elixir.pid (destructive)
```

## Quality & feedback

### `review_agent_feedback.py`
Print recent prompt failures and the 👍/👎 reaction feedback recorded against
agent replies. Useful for triaging what went wrong in production.

```bash
uv run --locked python scripts/review_agent_feedback.py --limit 20
uv run --locked python scripts/review_agent_feedback.py --workflow clanops
uv run --locked python scripts/review_agent_feedback.py --json --raw          # copy-paste into a model
uv run --locked python scripts/review_agent_feedback.py --include-positive    # also show 👍
```

## Eval harnesses

All three hit the real Claude API via `CLAUDE_API_KEY` (loaded from `.env`) and
the real local database. They write JSON to `scripts/*_results.json`, which is
gitignored.

### `eval_intent_router.py`
**Routing-only**, fast. Generates LLM questions across 10 categories
(clan_ops, own_deck, trophy_road, chat_noise, etc.) and runs each through
`classify_intent`. Tallies route distribution, confidence, fallbacks, and
suspicious classifications.

```bash
uv run --locked python scripts/eval_intent_router.py --rounds 2 --per-round 50
```

Use when you've changed the intent router prompt, added a route, or want to
stress edge cases without paying for full pipeline runs.

### `eval_deck_conversations.py`
**Deck pipeline depth-test.** Stratifies active members by war participation
(regular / occasional / rare / never), asks the LLM to write a 3-turn Discord
conversation tuned to each member's profile, then runs each turn through the
real deck workflow with tool-call capture and conversation-history carry.

```bash
uv run --locked python scripts/eval_deck_conversations.py --members 6 --turns 3
uv run --locked python scripts/eval_deck_conversations.py --members 6 --seed 42
```

Summary covers route + mode distribution, tool calls, errors, mode
inheritance on follow-ups, and war-suggest deck-count validation (expects 4).

### `eval_all_requests.py`
**Unified cross-bucket eval.** Three buckets per round:

- `regular`  — general Q&A about our clan/roster/gameplay (should route to
               llm_chat / kick_risk / clan_status / help; uses local tools)
- `deck`     — deck review/suggest/display (should route to a `deck_*` intent)
- `cr_api`   — external lookups with real CR tags (should fire the `cr_api`
               tool, possibly chained with `lookup_cards`)

Runs each prompt through the real pipeline (`respond_in_channel` or
`respond_in_deck_review`) and reports routing, tool usage, cr_api firing
rate on tag prompts, and output previews.

```bash
uv run --locked python scripts/eval_all_requests.py --rounds 2 --per-bucket 4
uv run --locked python scripts/eval_all_requests.py --rounds 1 --per-bucket 2 --seed 1   # smoke test
```

Tag fixtures (external clans, external players, our members) are sampled from
the local DB — no external seed files needed.

## Adding a new script

- Put operational utilities (anything that mutates prod state or is called by
  cron/launchd) at the top level of `scripts/`.
- Prefix eval harnesses with `eval_` and write their JSON output to
  `scripts/<name>_results.json`. Add the pattern to `.gitignore`.
- Document it in this README.
