"""The Editor — Elixir's internal output evaluation organ (editor.md).

Three parts, one module:
- the inline gate (`gate`) that delivery runs between compose and send:
  judge → pass | revise-once | fallback, ALWAYS fail-open — an Editor error
  can only ever let the original copy through, never block delivery;
- the living rubric: editorial memories (tags `editorial` +
  `exemplar`/`anti-pattern`) assembled fresh per verdict via the memory
  system's ranked retrieval, plus the auto-feeders that turn human actions
  (👎/👍, deletions, leader copy-edits) into rubric candidates;
- the weekly self-review composer (`compose_weekly_review`) behind the
  `editorial-review` activity.

The critic is one LLM call (workflow 'editorial', lightweight family —
editor.md §2), injectable via `critic_fn` so tests stub it and the offline
rehearsal never touches the network (the gate is dependency-injected into
delivery.consume by the live tick only).
"""

from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("elixir.engine.editor")

EDITOR_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS editor_verdicts (
    verdict_id INTEGER PRIMARY KEY,
    intent_id INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass','revise','fallback','error')),
    dimensions_json TEXT,
    original_copy TEXT, final_copy TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_editor_verdicts_intent ON editor_verdicts(intent_id);
"""

VERDICTS = ("pass", "revise", "fallback", "error")
DIMENSIONS = ("grounding", "substance", "freshness", "lane_fit")
RECENT_COPIES_LIMIT = 15
RUBRIC_LIMIT = 8
CANDIDATE_CONFIDENCE = 0.65   # auto-fed rubric candidates (feeders)
PROPOSED_CONFIDENCE = 0.6     # weekly-review proposals (editor.md §4)


def ensure_editor_schema(conn) -> None:
    conn.executescript(EDITOR_VERDICTS_DDL)


def enabled() -> bool:
    return os.getenv("ELIXIR_EDITOR_ENABLED", "1") != "0"


# ------------------------------------------------------------------ critic

_SYSTEM_PROMPT = """You are The Editor — the internal quality gate for Elixir, \
a Discord agent for the POAP KINGS Clash Royale clan. A post is about to go \
out. Judge it against four dimensions:

- grounding: every specific claim in the copy (numbers, possessions, events, \
achievements) must trace to the facts JSON. An invented detail — however \
plausible — is a hard failure.
- substance: would a human who actually looked at this member/moment write \
this? Copy that could have come from a blank template fails.
- freshness: no stock phrases recycled from Elixir's recent posts (compare \
against the recent-posts list).
- lane_fit: tone and length appropriate to the destination channel.

Rubric — learned from real posts (exemplars to emulate, anti-patterns to \
reject):
{rubric}

Respond with STRICT JSON only, no prose around it:
{{"verdict": "pass" | "revise" | "fallback",
  "dimensions": {{"grounding": {{"ok": true, "note": ""}},
                 "substance": {{"ok": true, "note": ""}},
                 "freshness": {{"ok": true, "note": ""}},
                 "lane_fit": {{"ok": true, "note": ""}}}},
  "critique": "one or two sentences: exactly what to fix (revise) or why it's unsalvageable (fallback); empty for pass"}}

verdict meanings — pass: sends as-is. revise: fixable with ONE rewrite; the \
critique must say precisely what to change. fallback: unsalvageable or the \
facts are too thin for anything beyond a deterministic template."""


def critic_llm(system: str, user: str) -> str:
    """The real critic call (workflow 'editorial' → lightweight family).
    Lazy import: engine modules stay light; only the live gate pays this."""
    from agent.core import _create_chat_completion, response_text

    resp = _create_chat_completion(
        workflow="editorial",
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=700,
        timeout=45,
    )
    return response_text(resp) or ""


# Injectable seam: tests replace this; the live gate uses critic_llm.
critic_fn = critic_llm


def parse_verdict(text: str) -> dict:
    """Tolerant parser for the strict-JSON verdict contract. Anything that
    doesn't yield a valid verdict maps to 'error' (→ fail-open upstream)."""
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {"verdict": "error", "dimensions": {}, "critique": "unparseable critic output"}
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return {"verdict": "error", "dimensions": {}, "critique": "invalid JSON from critic"}
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "revise", "fallback"):
        return {"verdict": "error", "dimensions": {}, "critique": f"unknown verdict {verdict!r}"}
    dims = data.get("dimensions")
    return {
        "verdict": verdict,
        "dimensions": dims if isinstance(dims, dict) else {},
        "critique": str(data.get("critique") or "").strip(),
    }


# ------------------------------------------------------------------ rubric

def build_rubric_context(conn, event_type: str | None, lane: str | None) -> str:
    """Assemble the rubric block from editorial memories via ranked retrieval
    (editor.md §3 — the rubric is data, not prompt text)."""
    import memory_store

    query = " ".join(p for p in ((event_type or "").replace("_", " "), lane or "") if p)
    try:
        rows = memory_store.select_memories(
            tags=["editorial"], query=query or None,
            viewer_scope="leadership", limit=RUBRIC_LIMIT, conn=conn,
        )
    except Exception:
        log.exception("rubric retrieval failed; judging without rubric")
        rows = []
    lines = []
    for row in rows:
        tags = set(row.get("tags") or [])
        kind = "ANTI-PATTERN" if "anti-pattern" in tags else (
            "EXEMPLAR" if "exemplar" in tags else "NOTE")
        body = " ".join((row.get("body") or "").split())
        lines.append(f"- [{kind}] {row.get('title')}: {body[:420]}")
    return "\n".join(lines) if lines else "(no rubric entries yet)"


def recent_copies(conn, limit: int = RECENT_COPIES_LIMIT) -> list[str]:
    """Recent outgoing copy for the freshness dimension — sourced from the
    verdicts trace (every gated post lands there)."""
    try:
        rows = conn.execute(
            """SELECT final_copy FROM editor_verdicts
               WHERE final_copy IS NOT NULL AND verdict != 'error'
               ORDER BY verdict_id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    except Exception:
        return []
    return [r["final_copy"] for r in rows if r["final_copy"]]


# ------------------------------------------------------------------- judge

def judge(copy: str, facts: dict, recent: list[str], rubric: str,
          lane: str | None, intent_type: str | None) -> dict:
    """One critic call → verdict dict. Any failure → verdict 'error'."""
    system = _SYSTEM_PROMPT.format(rubric=rubric or "(no rubric entries yet)")
    recent_block = "\n".join(f"- {c}" for c in recent) or "(none yet)"
    user = (
        f"Destination lane: {lane or 'unknown'}\n"
        f"Intent type: {intent_type or 'unknown'}\n\n"
        f"Facts JSON (the ONLY permissible source of specifics):\n"
        f"```json\n{json.dumps(facts, indent=2, default=str)}\n```\n\n"
        f"Elixir's recent posts (for the freshness check):\n{recent_block}\n\n"
        f"THE COPY TO JUDGE:\n{copy}"
    )
    try:
        return parse_verdict(critic_fn(system, user))
    except Exception as exc:
        log.warning("editor critic call failed (fail-open): %s", exc)
        return {"verdict": "error", "dimensions": {}, "critique": str(exc)[:200]}


def record_verdict(conn, intent_id: int, verdict: str, dimensions: dict | None,
                   original_copy: str | None, final_copy: str | None, now: str) -> int:
    cur = conn.execute(
        """INSERT INTO editor_verdicts
               (intent_id, verdict, dimensions_json, original_copy, final_copy, at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (int(intent_id), verdict,
         json.dumps(dimensions, default=str) if dimensions else None,
         original_copy, final_copy, now),
    )
    return cur.lastrowid


def attach_critique(intent, critique: str) -> dict:
    """A dict-shaped intent whose payload carries the editor critique —
    compose.intent_context surfaces it as a revision ask (signature of
    compose_fn stays unchanged)."""
    revised = dict(intent)
    try:
        payload = json.loads(revised.get("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    payload["editor_critique"] = critique
    revised["payload_json"] = json.dumps(payload, default=str)
    return revised


def _intent_facts(intent) -> dict:
    try:
        return json.loads(intent["payload_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def gate(conn, intent, copy: str, compose_fn, now: str) -> str:
    """The inline gate (editor.md §2). Returns the copy to send — original,
    revised-once, or the deterministic fallback. Fail-open: ANY internal
    error returns the original copy. Commits its verdict row immediately
    (delivery's per-intent commit discipline)."""
    from engine.recognition import compose as _compose

    if not enabled():
        return copy
    try:
        ensure_editor_schema(conn)
        facts = _intent_facts(intent)
        rubric = build_rubric_context(conn, facts.get("event_type"),
                                      intent["lane"])
        recent = recent_copies(conn)
        v1 = judge(copy, facts, recent, rubric, intent["lane"], intent["intent_type"])
        trace = {"round1": v1}

        if v1["verdict"] == "error":
            record_verdict(conn, intent["intent_id"], "error", trace, copy, copy, now)
            conn.commit()
            return copy  # fail-open
        if v1["verdict"] == "pass":
            record_verdict(conn, intent["intent_id"], "pass", trace, copy, copy, now)
            conn.commit()
            return copy
        if v1["verdict"] == "fallback":
            final = _compose.render_intent(intent)
            record_verdict(conn, intent["intent_id"], "fallback", trace, copy, final, now)
            conn.commit()
            return final

        # revise: ONE recompose with the critique attached, then re-judge.
        revised = None
        try:
            revised = compose_fn(attach_critique(intent, v1["critique"]))
        except Exception:
            log.exception("editor revise recompose failed for intent %s",
                          intent["intent_id"])
        if revised and not _compose.looks_like_meta(revised):
            v2 = judge(revised, facts, recent, rubric,
                       intent["lane"], intent["intent_type"])
            trace["round2"] = v2
            if v2["verdict"] in ("pass", "error"):  # error on round 2 → keep revision
                record_verdict(conn, intent["intent_id"], "revise", trace, copy, revised, now)
                conn.commit()
                return revised
        final = _compose.render_intent(intent)
        record_verdict(conn, intent["intent_id"], "fallback", trace, copy, final, now)
        conn.commit()
        return final
    except Exception:
        log.exception("editor gate failed for intent %s (fail-open)",
                      intent["intent_id"])
        try:
            conn.rollback()
        except Exception:
            pass
        return copy


# ---------------------------------------------------------------- feeders

def _editorial_memory_exists(conn, event_key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM memories WHERE source_event_key = ?", (event_key,)
    ).fetchone() is not None


def _add_editorial_memory(conn, *, title: str, body: str, kind_tag: str,
                          event_key: str, confidence: float, created_by: str,
                          extra_tags: tuple[str, ...] = (),
                          source_type: str = "inference") -> int | None:
    """One rubric entry, deduped on its event key. Returns memory_id or None."""
    import memory_store

    if _editorial_memory_exists(conn, event_key):
        return None
    et, _, eid = event_key.partition(":")
    mem = memory_store.create_memory(
        body=body, source_type=source_type,
        is_inference=(source_type == "inference"), confidence=confidence,
        created_by=created_by, scope="leadership", title=title,
        event_type=et, event_id=eid or None, conn=conn,
    )
    memory_store.attach_tags(
        mem["memory_id"], ["editorial", kind_tag, *extra_tags],
        actor=created_by, conn=conn,
    )
    return mem["memory_id"]


def sweep_prompt_feedback(conn, now: str | None = None) -> dict:
    """Daily feeder (editor.md §3): yesterday's 👎 → anti-pattern candidates,
    👍 → exemplar candidates, each with the post text. Deduped per feedback
    row; candidates carry the 'candidate' tag and feeder confidence."""
    counters = {"anti_pattern": 0, "exemplar": 0, "skipped": 0}
    rows = conn.execute(
        """SELECT prompt_feedback_id, feedback_value, response_preview,
                  channel_name, workflow
           FROM prompt_feedback
           WHERE removed_at IS NULL AND response_preview IS NOT NULL
             AND updated_at >= strftime('%Y-%m-%dT%H:%M:%S', COALESCE(?, 'now'), '-1 day')""",
        (now,),
    ).fetchall()
    for r in rows:
        text = (r["response_preview"] or "").strip()
        if not text:
            counters["skipped"] += 1
            continue
        kind_tag = "anti-pattern" if r["feedback_value"] == "down" else "exemplar"
        verb = "flagged 👎" if kind_tag == "anti-pattern" else "endorsed 👍"
        added = _add_editorial_memory(
            conn,
            title=f"{kind_tag.title()} candidate: reader {verb} a post",
            body=(f"A reader {verb} this Elixir post in #{r['channel_name'] or '?'} "
                  f"(workflow {r['workflow'] or '?'}):\n\n{text[:600]}"),
            kind_tag=kind_tag,
            event_key=f"editorial_feedback:{r['prompt_feedback_id']}",
            confidence=CANDIDATE_CONFIDENCE,
            created_by="editor-sweep",
            extra_tags=("candidate",),
        )
        if added:
            counters[kind_tag.replace("-", "_")] += 1
        else:
            counters["skipped"] += 1
    conn.commit()
    return counters


def record_deleted_post(conn, discord_message_id: str, channel_name: str,
                        content: str) -> int | None:
    """Deletion feeder (editor.md §3): an admin deleting an Elixir post in
    one of its lanes is the strongest anti-pattern signal we get."""
    intent = conn.execute(
        "SELECT intent_type, payload_json FROM communication_intents "
        "WHERE discord_message_id = ?",
        (str(discord_message_id),),
    ).fetchone()
    facts_note = ""
    if intent:
        facts_note = (f"\n\nIntent type: {intent['intent_type']}\n"
                      f"Facts it was composed from: {intent['payload_json'][:500]}")
    mid = _add_editorial_memory(
        conn,
        title="Anti-pattern: post deleted by an admin",
        body=(f"An admin deleted this Elixir post in #{channel_name}:\n\n"
              f"{(content or '(content unavailable)')[:600]}{facts_note}"),
        kind_tag="anti-pattern",
        event_key=f"editorial_deletion:{discord_message_id}",
        confidence=0.75,
        created_by="editor-deletion",
    )
    conn.commit()
    return mid


def record_copy_edit_pair(conn, action_id: int, before: str, after: str) -> int | None:
    """Copy-edit feeder (editor.md §3): a leader rewriting action-card copy is
    a paired before/after exemplar — the after is what good looks like."""
    before, after = (before or "").strip(), (after or "").strip()
    if not before or not after or before == after:
        return None
    mid = _add_editorial_memory(
        conn,
        title="Exemplar pair: leader copy-edit on an action card",
        body=(f"A leader rewrote Elixir's action-card copy — the AFTER is the "
              f"standard to emulate.\n\nBEFORE (Elixir): {before[:400]}\n\n"
              f"AFTER (leader): {after[:400]}"),
        kind_tag="exemplar",
        event_key=f"editorial_copy_edit:{action_id}",
        confidence=0.8,
        created_by="editor-copy-edit",
    )
    conn.commit()
    return mid


# ----------------------------------------------------------- weekly review

_REVIEW_SYSTEM = """You are The Editor doing Elixir's weekly editorial \
self-review. You get the week's gate verdicts and a sample of posted copy. \
Assess the week's output against the rubric dimensions (grounding, substance, \
freshness, lane fit) and respond with STRICT JSON only:
{"assessment": "3-5 sentences on the week's editorial quality",
 "drift_line": "ONE plain-words sentence: how far this week's output drifted from the exemplar class",
 "proposed_rubric_entries": [{"kind": "exemplar" | "anti-pattern", "title": "...", "body": "quote the post text and say why"}]}
Propose at most 3 entries, only when a concrete post this week deserves to \
become a permanent rubric example. An empty list is a fine answer."""


def compose_weekly_review(conn, now: str) -> dict:
    """Gather the week → one LLM call → synthesis memory + report text
    (editor.md §4). Proposed entries auto-add at confidence 0.6, listed in
    the report for Jamie's veto."""
    ensure_editor_schema(conn)
    week_ago = "strftime('%Y-%m-%dT%H:%M:%S', ?, '-7 days')"
    verdict_counts = {
        r["verdict"]: r["n"] for r in conn.execute(
            f"SELECT verdict, COUNT(*) n FROM editor_verdicts WHERE at >= {week_ago} "
            "GROUP BY verdict", (now,)).fetchall()
    }
    fulfilled = conn.execute(
        f"""SELECT intent_type, lane FROM communication_intents
            WHERE status = 'fulfilled' AND fulfilled_at >= {week_ago}""",
        (now,),
    ).fetchall()
    sample = conn.execute(
        f"""SELECT v.verdict, v.final_copy, i.intent_type, i.lane
            FROM editor_verdicts v
            JOIN communication_intents i ON i.intent_id = v.intent_id
            WHERE v.at >= {week_ago} AND v.final_copy IS NOT NULL
            ORDER BY v.verdict_id DESC LIMIT 20""",
        (now,),
    ).fetchall()
    gated = sum(verdict_counts.values())
    revision_rate = (
        (verdict_counts.get("revise", 0) + verdict_counts.get("fallback", 0)) / gated
        if gated else 0.0
    )
    sample_block = "\n".join(
        f"- [{r['verdict']}] ({r['intent_type']} → {r['lane']}) {(r['final_copy'] or '')[:220]}"
        for r in sample) or "(no gated posts this week)"
    user = (
        f"Week ending {now}.\n"
        f"Fulfilled intents: {len(fulfilled)}.\n"
        f"Gate verdicts: {json.dumps(verdict_counts) or '{}'} "
        f"(revision rate {revision_rate:.0%}).\n\n"
        f"Sample of the week's gated copy:\n{sample_block}"
    )
    try:
        parsed_raw = critic_fn(_REVIEW_SYSTEM, user)
        start, end = parsed_raw.find("{"), parsed_raw.rfind("}")
        parsed = json.loads(parsed_raw[start:end + 1]) if start >= 0 else {}
    except Exception:
        log.exception("editorial review LLM call failed")
        parsed = {}
    assessment = str(parsed.get("assessment") or "Review call failed; counts only.")
    drift_line = str(parsed.get("drift_line") or "(no drift assessment)")

    added_titles = []
    for entry in (parsed.get("proposed_rubric_entries") or [])[:3]:
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in ("exemplar", "anti-pattern"):
            continue
        title = str(entry.get("title") or "").strip()[:120]
        body = str(entry.get("body") or "").strip()
        if not title or not body:
            continue
        key = f"editorial_review_proposal:{now[:10]}:{len(added_titles)}"
        if _add_editorial_memory(conn, title=title, body=body, kind_tag=kind,
                                 event_key=key, confidence=PROPOSED_CONFIDENCE,
                                 created_by="editorial-review",
                                 extra_tags=("proposed",)):
            added_titles.append(f"[{kind}] {title}")

    import memory_store
    synthesis = memory_store.create_memory(
        body=(f"Weekly editorial self-review ({now[:10]}). {assessment} "
              f"Drift: {drift_line} Gate: {json.dumps(verdict_counts)}; "
              f"revision rate {revision_rate:.0%} over {gated} gated posts."),
        source_type="synthesis", is_inference=False, confidence=0.9,
        created_by="editorial-review", scope="leadership",
        title=f"Editorial self-review — week ending {now[:10]}",
        event_type="editorial_review", event_id=now[:10], conn=conn,
    )
    memory_store.attach_tags(synthesis["memory_id"],
                             ["editorial", "weekly-review"],
                             actor="editorial-review", conn=conn)
    conn.commit()

    proposals_block = (
        "\n".join(f"  • {t}" for t in added_titles)
        if added_titles else "  • none this week"
    )
    report = (
        f"**Editorial self-review — week ending {now[:10]}**\n"
        f"Posts fulfilled: {len(fulfilled)} · gated: {gated} · "
        f"verdicts: {json.dumps(verdict_counts) if verdict_counts else 'none'} · "
        f"revision rate: {revision_rate:.0%}\n"
        f"**Drift:** {drift_line}\n"
        f"{assessment}\n"
        f"**Proposed rubric additions** (auto-added at confidence 0.6 — "
        f"👎 this report or say the word to retire any):\n{proposals_block}"
    )
    return {"report": report, "verdict_counts": verdict_counts,
            "proposals": added_titles, "synthesis_id": synthesis["memory_id"]}
