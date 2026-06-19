"""Elixir improvement suggestion ledger.

The improvement ledger is intentionally one layer before GitHub. Elixir can
observe and store maintainer suggestions in shadow mode, then a separate
promotion step decides whether a suggestion becomes a public issue.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Optional

import db as _db
from db import managed_connection

SUGGESTION_SHADOW = "shadow"
SUGGESTION_PROMOTED = "promoted"
SUGGESTION_DISMISSED = "dismissed"
SUGGESTION_IMPLEMENTED = "implemented"

SUGGESTION_STATUSES = {
    SUGGESTION_SHADOW,
    SUGGESTION_PROMOTED,
    SUGGESTION_DISMISSED,
    SUGGESTION_IMPLEMENTED,
}

SUGGESTION_CATEGORIES = {
    "signal_gap",
    "routing_quality",
    "decision_drift",
    "coaching_opportunity",
    "data_health",
    "cost_reliability",
    "backlog_hygiene",
}

__all__ = [
    "SUGGESTION_CATEGORIES",
    "SUGGESTION_DISMISSED",
    "SUGGESTION_IMPLEMENTED",
    "SUGGESTION_PROMOTED",
    "SUGGESTION_SHADOW",
    "SUGGESTION_STATUSES",
    "build_improvement_github_issue_body",
    "github_labels_for_improvement",
    "get_improvement_suggestion",
    "list_improvement_suggestions",
    "mark_improvement_suggestion_promoted",
    "suggestion_key_for",
    "upsert_improvement_suggestion",
]


def _clean_text(value) -> str:
    return " ".join(str(value or "").split())


def _json_dumps(value) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str, ensure_ascii=False)


def _loads_dict(value) -> dict:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:80].strip("-") or "suggestion"


def _normalize_status(status: str | None) -> str:
    clean = _clean_text(status) or SUGGESTION_SHADOW
    if clean not in SUGGESTION_STATUSES:
        raise ValueError(f"invalid suggestion status: {clean}")
    return clean


def _normalize_category(category: str | None) -> str:
    clean = _clean_text(category) or "data_health"
    if clean not in SUGGESTION_CATEGORIES:
        raise ValueError(f"invalid suggestion category: {clean}")
    return clean


def _clamp_severity(value) -> int:
    try:
        return max(1, min(int(value), 5))
    except (TypeError, ValueError):
        return 3


def _clamp_confidence(value) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.5


def _source_fingerprint(evidence: dict) -> str:
    return hashlib.sha256(_json_dumps(evidence).encode("utf-8")).hexdigest()


def suggestion_key_for(category: str, title: str, *, basis: str | None = None) -> str:
    """Return a stable suggestion key for a category/title/basis tuple."""
    clean_category = _normalize_category(category)
    clean_title = _clean_text(title)
    digest_basis = "\n".join([clean_category, clean_title, _clean_text(basis)])
    digest = hashlib.sha256(digest_basis.encode("utf-8")).hexdigest()[:12]
    return f"{clean_category}:{_slug(clean_title)}:{digest}"


def _row_to_suggestion(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["evidence"] = _loads_dict(item.pop("evidence_json", "{}"))
    return item


@managed_connection
def upsert_improvement_suggestion(
    *,
    category: str,
    title: str,
    rationale: str,
    proposed_change: str,
    evidence: dict | None = None,
    suggestion_key: str | None = None,
    status: str = SUGGESTION_SHADOW,
    severity: int = 3,
    confidence: float = 0.5,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Create or update a shadow suggestion idempotently."""
    clean_category = _normalize_category(category)
    clean_title = _clean_text(title)
    clean_rationale = _clean_text(rationale)
    clean_change = _clean_text(proposed_change)
    if not clean_title or not clean_rationale or not clean_change:
        raise ValueError("title, rationale, and proposed_change are required")
    normalized_evidence = evidence or {}
    key = _clean_text(suggestion_key) or suggestion_key_for(
        clean_category,
        clean_title,
        basis=normalized_evidence.get("basis") or normalized_evidence.get("source"),
    )
    now = _db._utcnow()
    conn.execute(
        """
        INSERT INTO elixir_improvement_suggestions (
            suggestion_key, category, status, severity, confidence, title,
            rationale, proposed_change, evidence_json, source_fingerprint,
            first_seen_at, last_seen_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(suggestion_key) DO UPDATE SET
            category = excluded.category,
            status = CASE
                WHEN elixir_improvement_suggestions.status IN ('promoted', 'dismissed', 'implemented')
                    THEN elixir_improvement_suggestions.status
                ELSE excluded.status
            END,
            severity = excluded.severity,
            confidence = MAX(elixir_improvement_suggestions.confidence, excluded.confidence),
            title = excluded.title,
            rationale = excluded.rationale,
            proposed_change = excluded.proposed_change,
            evidence_json = excluded.evidence_json,
            source_fingerprint = excluded.source_fingerprint,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (
            key,
            clean_category,
            _normalize_status(status),
            _clamp_severity(severity),
            _clamp_confidence(confidence),
            clean_title,
            clean_rationale,
            clean_change,
            _json_dumps(normalized_evidence),
            _source_fingerprint(normalized_evidence),
            now,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return get_improvement_suggestion(key, conn=conn) or {}


@managed_connection
def get_improvement_suggestion(
    suggestion_key: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM elixir_improvement_suggestions WHERE suggestion_key = ?",
        (_clean_text(suggestion_key),),
    ).fetchone()
    return _row_to_suggestion(row)


@managed_connection
def list_improvement_suggestions(
    *,
    status: str | None = None,
    category: str | None = None,
    min_confidence: float | None = None,
    unpromoted_only: bool = False,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(_normalize_status(status))
    if category:
        clauses.append("category = ?")
        params.append(_normalize_category(category))
    if min_confidence is not None:
        clauses.append("confidence >= ?")
        params.append(_clamp_confidence(min_confidence))
    if unpromoted_only:
        clauses.append("github_issue_number IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM elixir_improvement_suggestions {where} "
        "ORDER BY severity DESC, confidence DESC, updated_at DESC, suggestion_id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 50), 200))),
    ).fetchall()
    return [item for row in rows if (item := _row_to_suggestion(row))]


@managed_connection
def mark_improvement_suggestion_promoted(
    suggestion_key: str,
    *,
    github_issue_number: int,
    github_issue_url: str | None = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict | None:
    now = _db._utcnow()
    conn.execute(
        """
        UPDATE elixir_improvement_suggestions
        SET status = ?, github_issue_number = ?, github_issue_url = ?,
            promoted_at = COALESCE(promoted_at, ?), updated_at = ?
        WHERE suggestion_key = ?
        """,
        (
            SUGGESTION_PROMOTED,
            int(github_issue_number),
            _clean_text(github_issue_url) or None,
            now,
            now,
            _clean_text(suggestion_key),
        ),
    )
    conn.commit()
    return get_improvement_suggestion(suggestion_key, conn=conn)


_CATEGORY_LABELS = {
    "signal_gap": "signal-gap",
    "routing_quality": "routing-quality",
    "decision_drift": "routing-quality",
    "coaching_opportunity": "coaching",
    "data_health": "data-health",
    "cost_reliability": "cost-reliability",
    "backlog_hygiene": "backlog-hygiene",
}


def github_labels_for_improvement(suggestion: dict) -> list[str]:
    labels = ["enhancement", "elixir-improvement", "generated", "needs-human-triage"]
    category = (suggestion or {}).get("category")
    category_label = _CATEGORY_LABELS.get(category)
    if category_label:
        labels.append(category_label)
    return sorted(dict.fromkeys(labels))


def build_improvement_github_issue_body(suggestion: dict) -> str:
    evidence = suggestion.get("evidence") or {}
    evidence_lines = []
    for item in evidence.get("samples") or []:
        if not isinstance(item, dict):
            continue
        label = _clean_text(item.get("label") or item.get("type") or "evidence")
        detail = _clean_text(item.get("detail") or item.get("summary") or item.get("note"))
        if detail:
            evidence_lines.append(f"- {label}: {detail}")
    metrics = evidence.get("metrics") if isinstance(evidence.get("metrics"), dict) else {}
    metric_lines = [f"- {key}: {value}" for key, value in sorted(metrics.items())]
    if not evidence_lines and evidence.get("summary"):
        evidence_lines.append(f"- summary: {_clean_text(evidence.get('summary'))}")
    if not evidence_lines:
        evidence_lines.append("- No compact evidence samples were stored.")
    body = [
        f"<!-- elixir-suggestion-key: {suggestion.get('suggestion_key')} -->",
        "",
        "## Elixir Improvement Suggestion",
        "",
        f"**Category:** `{suggestion.get('category')}`",
        f"**Severity:** {suggestion.get('severity')}",
        f"**Confidence:** {suggestion.get('confidence')}",
        f"**First seen:** {suggestion.get('first_seen_at')}",
        f"**Last seen:** {suggestion.get('last_seen_at')}",
        "",
        "## Rationale",
        "",
        suggestion.get("rationale") or "",
        "",
        "## Proposed Change",
        "",
        suggestion.get("proposed_change") or "",
        "",
        "## Evidence",
        "",
        *evidence_lines[:12],
    ]
    if metric_lines:
        body.extend(["", "## Metrics", "", *metric_lines[:12]])
    body.extend([
        "",
        "## Triage",
        "",
        "- [ ] Human reviewed the suggestion",
        "- [ ] Scope accepted, revised, or closed as not planned",
    ])
    return "\n".join(body).strip() + "\n"
