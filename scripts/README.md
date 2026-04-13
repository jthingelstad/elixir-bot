# scripts/

Operational utilities and eval harnesses for the Elixir bot.

Run everything from the repo root with the project venv active:

```bash
source venv/bin/activate
python scripts/<name>.py [args]
```

## Operations

### `admin.sh`
Service control for the launchd agent (`com.poapkings.elixir`).

```bash
scripts/admin.sh install    # write ~/Library/LaunchAgents/com.poapkings.elixir.plist
scripts/admin.sh start      # launchctl bootstrap
scripts/admin.sh stop       # launchctl bootout
scripts/admin.sh restart
scripts/admin.sh status
scripts/admin.sh upgrade    # stop → git pull → pip install -r → start
scripts/admin.sh backup     # invokes backup_db.py
```

### `backup_db.py`
Safe online SQLite backup (uses `sqlite3.Connection.backup()` — no need to stop
the bot) plus tiered retention pruning. Also imported by the db-maintenance
job.

```bash
python scripts/backup_db.py
```

- Output: `~/elixir-backups/elixir-YYYY-MM-DD-HHMMSS.db.gz` (gzip level 6)
- Integrity-checks the snapshot before compressing
- Retention: keep-all ≤28d · monthly 29–90d · quarterly 91–365d · delete >365d

Override via env:
- `ELIXIR_DB_PATH` — source database (default: `<repo>/elixir.db`)
- `ELIXIR_BACKUP_DIR` — destination dir (default: `~/elixir-backups`)

### `clean.py`
Remove local cache/build cruft.

```bash
python scripts/clean.py           # removes __pycache__, .pytest_cache, .mypy_cache, .ruff_cache
python scripts/clean.py --db      # also removes elixir.db and elixir.pid (destructive)
```

## Quality & feedback

### `review_agent_feedback.py`
Print recent prompt failures and the 👍/👎 reaction feedback recorded against
agent replies. Useful for triaging what went wrong in production.

```bash
python scripts/review_agent_feedback.py --limit 20
python scripts/review_agent_feedback.py --workflow clanops
python scripts/review_agent_feedback.py --json --raw          # copy-paste into a model
python scripts/review_agent_feedback.py --include-positive    # also show 👍
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
python scripts/eval_intent_router.py --rounds 2 --per-round 50
```

Use when you've changed the intent router prompt, added a route, or want to
stress edge cases without paying for full pipeline runs.

### `eval_deck_conversations.py`
**Deck pipeline depth-test.** Stratifies active members by war participation
(regular / occasional / rare / never), asks the LLM to write a 3-turn Discord
conversation tuned to each member's profile, then runs each turn through the
real deck workflow with tool-call capture and conversation-history carry.

```bash
python scripts/eval_deck_conversations.py --members 6 --turns 3
python scripts/eval_deck_conversations.py --members 6 --seed 42
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
python scripts/eval_all_requests.py --rounds 2 --per-bucket 4
python scripts/eval_all_requests.py --rounds 1 --per-bucket 2 --seed 1   # smoke test
```

Tag fixtures (external clans, external players, our members) are sampled from
the local DB — no external seed files needed.

## Adding a new script

- Put operational utilities (anything that mutates prod state or is called by
  cron/launchd) at the top level of `scripts/`.
- Prefix eval harnesses with `eval_` and write their JSON output to
  `scripts/<name>_results.json`. Add the pattern to `.gitignore`.
- Document it in this README.
