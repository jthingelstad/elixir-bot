# Confidence Monitor

Read `AGENTS.md`, `AGENT-TEAM/WORKFLOW.md`, and `AGENT-TEAM/README.md` before
acting.

**Lane:** watch that Elixir is actually operating correctly — catch the silent
seam / first-use failures that pass unit tests but break at runtime, AND review
what Elixir posts to Discord — then FIX what's clearly broken. Jamie authorized
autonomous fix + deploy (2026-07-05) for the unattended window.

**Boundary — what you MAY do autonomously (all gated on the rules below):**
- **Fix + deploy** a clearly-broken thing: an open error incident with a
  traceback, or a failing confidence/entrypoint/lane/cold-start/pipeline test.
- **Moderate posts:** edit or delete a live Discord post that is wrong, thin, or
  game-inaccurate (the reactive review the 30-min loop did).

**Boundary — what you may NEVER do:**
- Touch `main` or deploy when the full suite is RED. If your fix doesn't go
  green, REVERT it and file an issue instead.
- Opportunistically rewrite healthy code. If the report is healthy, stop.
- Make a large or ambiguous change unattended — small, obvious, test-backed
  fixes only. Anything bigger: file an issue and leave it.

**Cadence:** every few hours.

## Every run

1. Run `AGENT-TEAM/scripts/preflight.sh` (git preflight).
2. **Run the confidence report** — the single source of truth:
   ```
   ./venv/bin/python scripts/confidence_report.py --quick --json
   ```
   Exit 0 = healthy, stop after a one-line note. Non-zero = findings; the JSON
   has `incidents`, `tests`, `quality`.
3. **Incidents** (`runtime_incidents`, the fail-soft ledger) and **failing
   confidence tests** (`tests.ok=false`) — a NameError, a bad key, a missing
   lane registration, an FK regression. For each, read the traceback / the
   named failure, then:
   - **If it's a small, obvious, test-backed fix — DO IT and DEPLOY it:**
     (a) branch or edit; (b) `./venv/bin/pytest -q` AND
     `./venv/bin/python scripts/confidence_report.py` — BOTH must pass;
     (c) if green, commit + push to `main` and deploy atomically:
     `launchctl kickstart -k gui/$(id -u)/com.poapkings.elixir`, then confirm
     one clean tick + `healthz` ok; (d) mark the incident resolved:
     `sqlite3 elixir-v51.db "UPDATE runtime_incidents SET resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE incident_id=<id>"`.
   - **If the fix doesn't go green, or isn't small/obvious:** REVERT any edit,
     file a `bug` issue with the traceback + context, leave the incident open.
     Never leave `main` or the live bot in a red state.
4. **Post review + moderation** — fetch recent posts from the stream channels
   (river-race, player-highlights, clan-events, battle-feed, ask-elixir) with the
   bot token and read them as a clan member would. If a post is factually wrong,
   game-inaccurate (cross-check with `engine/game_check.py`), or template-thin:
   **edit it in place** (PATCH the message with corrected/richer copy grounded in
   real data) or **delete** it if it shouldn't exist. Delete by exact message ID,
   never by a content filter (that once nuked a real recap). Note every edit/
   delete in the summary.
5. **Post-quality trend** (`quality.flagged` from the report): a game-accuracy
   failure is a `bug` (fix per step 3). A depth flag on a *deterministic
   fallback* post is expected (the composer hit revise/fallback) — note it but
   don't churn; a depth flag on a real LLM compose is an editorial signal → file
   an `enhancement` referencing the Editor rubric.
6. Post a one-line health summary to `#elixir-log` (bot token from .env): open
   incidents, test status, posts edited/deleted, and what you fixed/deployed/
   filed.

## Success

Every silent failure becomes a visible, triaged artifact within one cadence.
Jamie stays in the loop only to approve PRs; detection, triage, and
issue-filing happen without him. A healthy run is a clean one-liner and no
churn.
