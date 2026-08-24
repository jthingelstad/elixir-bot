"""Fastmail JMAP mail client for Elixir (ported from Oliver's agent/mail/email_jmap.py).

The Fastmail account is shared across several agent identities (Otto, Thingy,
Oliver, Elixir); Elixir sends as ELIXIR_EMAIL_ADDRESS and stores sent mail in
Sent/Elixir. This is the send path only — inbound reading lives elsewhere if it
is ever needed. Config comes straight from the environment.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from email.utils import getaddresses
from functools import cached_property
from typing import Any

import requests

from agent.mail import email_render

log = logging.getLogger("elixir.email")

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"

# ── Config (env-driven; .env is loaded by the runtime and cut_release.py) ──
JMAP_TOKEN = os.getenv("FASTMAIL_JMAP_TOKEN")
JMAP_SESSION_URL = os.getenv("FASTMAIL_JMAP_SESSION_URL", "https://api.fastmail.com/jmap/session")
EMAIL_ADDRESS = os.getenv("ELIXIR_EMAIL_ADDRESS", "elixir@poapkings.com")
EMAIL_FROM_NAME = os.getenv("ELIXIR_EMAIL_FROM_NAME", "Elixir")
SENT_PARENT = os.getenv("ELIXIR_EMAIL_SENT_PARENT", "Sent")
# The shared mailbox uses a per-agent "<Agent>-Sent" scheme (Elixir-Sent,
# Oliver-Sent, Otto-Sent, Thingy-Sent). Elixir was missed when the folders were
# renamed — Oliver's defaults were aligned but these were left as "Elixir", so
# every send raised JMAPError("Could not find mailbox Sent/Elixir") and the
# weekly clan recap + member report emails silently stopped going out.
SENT_FOLDER = os.getenv("ELIXIR_EMAIL_SENT_FOLDER", "Elixir-Sent")

_TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_JMAP_ERRORS = {"serverFail", "serverUnavailable"}
_STATE_MISMATCH = "stateMismatch"


class JMAPError(RuntimeError):
    """Raised for missing config, malformed JMAP state, or server-side errors."""


class JMAPHTTPError(JMAPError):
    """An HTTP response from the JMAP endpoint was not successful."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Fastmail HTTP {status_code}: {detail}")
        self.status_code = status_code


class JMAPMethodError(JMAPError):
    """A JMAP method-level error response."""

    def __init__(self, call_id: str, payload: dict[str, Any]) -> None:
        super().__init__(f"JMAP call {call_id} failed: {payload}")
        self.call_id = call_id
        self.payload = payload
        self.error_type = str(payload.get("type") or "")


@dataclass(frozen=True)
class MailFolders:
    sent_elixir: str
    drafts: str


def enabled() -> bool:
    """True when a token is configured — callers keep email best-effort."""
    return bool(JMAP_TOKEN)


class JMAPClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        session_url: str | None = None,
        timeout: float = 30.0,
        retry_delays: tuple[float, ...] = (1.0, 3.0),
    ) -> None:
        self.token = token or JMAP_TOKEN
        self.session_url = session_url or JMAP_SESSION_URL
        self.timeout = timeout
        self.retry_delays = retry_delays
        if not self.token:
            raise JMAPError("FASTMAIL_JMAP_TOKEN is not configured")

    @cached_property
    def session(self) -> dict[str, Any]:
        attempts = len(self.retry_delays) + 1
        for attempt in range(attempts):
            try:
                r = requests.get(
                    self.session_url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=self.timeout,
                )
                self._raise_for_response(r)
                return r.json()
            except (requests.Timeout, requests.ConnectionError, JMAPError) as exc:
                if not self._should_retry(exc, attempt, attempts):
                    raise
                self._wait_before_retry("session", attempt, attempts, exc)
        raise AssertionError("unreachable")

    @property
    def api_url(self) -> str:
        return self.session["apiUrl"]

    @property
    def mail_account_id(self) -> str:
        return self._account_id(MAIL)

    @property
    def submission_account_id(self) -> str:
        return self._account_id(SUBMISSION)

    def _account_id(self, capability: str) -> str:
        primary = self.session.get("primaryAccounts", {})
        if primary.get(capability):
            return primary[capability]
        accounts = self.session.get("accounts", {})
        for account_id, account in accounts.items():
            if capability in account.get("accountCapabilities", {}):
                return account_id
        raise JMAPError(f"No account supports {capability}")

    def call(
        self,
        method_calls: list[list[Any]],
        *,
        using: list[str] | None = None,
        retry_transient: bool = False,
        operation: str = "call",
    ) -> list[list[Any]]:
        body = {"using": using or [CORE, MAIL, SUBMISSION], "methodCalls": method_calls}
        attempts = len(self.retry_delays) + 1 if retry_transient else 1
        for attempt in range(attempts):
            try:
                r = requests.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    json=body,
                    timeout=self.timeout,
                )
                self._raise_for_response(r)
                data = r.json()
                if data.get("methodResponses") is None:
                    raise JMAPError(f"Malformed JMAP response: {data!r}")
                for method, payload, call_id in data["methodResponses"]:
                    if method == "error":
                        raise JMAPMethodError(call_id, payload)
                return data["methodResponses"]
            except (requests.Timeout, requests.ConnectionError, JMAPError) as exc:
                if not retry_transient or not self._should_retry(exc, attempt, attempts):
                    raise
                self._wait_before_retry(operation, attempt, attempts, exc)
        raise AssertionError("unreachable")

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, JMAPHTTPError):
            return exc.status_code in _TRANSIENT_HTTP_STATUSES
        if isinstance(exc, JMAPMethodError):
            return exc.error_type in _TRANSIENT_JMAP_ERRORS
        return False

    def _should_retry(self, exc: Exception, attempt: int, attempts: int) -> bool:
        return attempt + 1 < attempts and self._is_transient(exc)

    def _wait_before_retry(
        self, operation: str, attempt: int, attempts: int, exc: Exception
    ) -> None:
        delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
        log.warning(
            "Fastmail JMAP retry operation=%s attempt=%d/%d delay=%.1fs error=%s",
            operation,
            attempt + 2,
            attempts,
            delay,
            type(exc).__name__,
        )
        time.sleep(delay)

    @staticmethod
    def _raise_for_response(response: requests.Response) -> None:
        if response.status_code >= 400:
            text = response.text[:500]
            raise JMAPHTTPError(response.status_code, text)

    @cached_property
    def folders(self) -> MailFolders:
        rows = self._mailboxes()
        by_id = {m["id"]: m for m in rows}
        sent_parent = self._find_parent(rows, SENT_PARENT, "sent")
        drafts = self._find_parent(rows, "Drafts", "drafts")
        sent_elixir = self._find_child(
            rows,
            by_id,
            sent_parent["id"],
            SENT_FOLDER,
            f"{SENT_PARENT}/{SENT_FOLDER}",
        )
        return MailFolders(sent_elixir=sent_elixir["id"], drafts=drafts["id"])

    def _mailboxes(self) -> list[dict[str, Any]]:
        responses = self.call(
            [
                [
                    "Mailbox/get",
                    {
                        "accountId": self.mail_account_id,
                        "ids": None,
                        "properties": ["id", "name", "parentId", "role"],
                    },
                    "mailboxes",
                ],
            ],
            using=[CORE, MAIL],
            retry_transient=True,
            operation="mailbox-read",
        )
        return responses[0][1]["list"]

    @staticmethod
    def _find_parent(rows: list[dict[str, Any]], name: str, role: str) -> dict[str, Any]:
        role_match = next((m for m in rows if m.get("role") == role), None)
        if role_match:
            return role_match
        name_match = next(
            (m for m in rows if m.get("parentId") is None and m.get("name") == name),
            None,
        )
        if name_match:
            return name_match
        raise JMAPError(f"Could not find {name} mailbox")

    @staticmethod
    def _find_child(
        rows: list[dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
        parent_id: str,
        name: str,
        path: str,
    ) -> dict[str, Any]:
        child = next(
            (m for m in rows if m.get("parentId") == parent_id and m.get("name") == name),
            None,
        )
        if child:
            return child
        flat = next((m for m in rows if m.get("name") == path), None)
        if flat and (flat.get("parentId") is None or flat.get("parentId") in by_id):
            return flat
        raise JMAPError(f"Could not find mailbox {path}")

    @cached_property
    def identity_id(self) -> str:
        responses = self.call(
            [
                [
                    "Identity/get",
                    {
                        "accountId": self.submission_account_id,
                        "ids": None,
                        "properties": ["id", "name", "email"],
                    },
                    "identities",
                ],
            ],
            using=[CORE, SUBMISSION],
            retry_transient=True,
            operation="identity-read",
        )
        identities = responses[0][1]["list"]
        wanted = EMAIL_ADDRESS.lower()
        match = next((i for i in identities if (i.get("email") or "").lower() == wanted), None)
        if not match and identities:
            log.warning("No identity for %s; using first configured Fastmail identity", wanted)
            match = identities[0]
        if not match:
            raise JMAPError("No JMAP sending identities are configured")
        return match["id"]

    def _object_state(self, data_type: str, account_id: str, using: list[str]) -> str:
        responses = self.call(
            [
                [
                    f"{data_type}/get",
                    {"accountId": account_id, "ids": []},
                    "state",
                ]
            ],
            using=using,
            retry_transient=True,
            operation=f"{data_type.lower()}-state-read",
        )
        state = responses[0][1].get("state")
        if not state:
            raise JMAPError(f"{data_type}/get returned no state")
        return str(state)

    def _get_email(self, email_id: str) -> dict[str, Any] | None:
        responses = self.call(
            [
                [
                    "Email/get",
                    {
                        "accountId": self.mail_account_id,
                        "ids": [email_id],
                        "properties": ["id", "threadId", "mailboxIds", "messageId"],
                    },
                    "email",
                ]
            ],
            using=[CORE, MAIL],
            retry_transient=True,
            operation="email-read",
        )
        rows = responses[0][1].get("list") or []
        return rows[0] if rows else None

    def _find_email_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        responses = self.call(
            [
                [
                    "Email/query",
                    {
                        "accountId": self.mail_account_id,
                        "filter": {"header": ["Message-ID", message_id]},
                        "limit": 10,
                    },
                    "email-query",
                ]
            ],
            using=[CORE, MAIL],
            retry_transient=True,
            operation="email-reconcile-query",
        )
        ids = responses[0][1].get("ids") or []
        matches = []
        for email_id in ids:
            email = self._get_email(str(email_id))
            if email and message_id in (email.get("messageId") or []):
                matches.append(email)
        if not matches:
            return None
        return next(
            (
                email
                for email in matches
                if (email.get("mailboxIds") or {}).get(self.folders.drafts)
            ),
            matches[0],
        )

    def _email_is_sent(self, email_id: str) -> bool:
        email = self._get_email(email_id)
        if not email:
            return False
        mailbox_ids = email.get("mailboxIds") or {}
        return bool(mailbox_ids.get(self.folders.sent_elixir))

    def _create_draft(self, email_obj: dict[str, Any], message_id: str) -> dict[str, Any]:
        """Create exactly one draft across ambiguous transport failures.

        Every retry carries the same Email state token. If Fastmail committed the
        first request but its response was lost, the retry is rejected with
        ``stateMismatch``; the Message-ID then resolves the already-created draft.
        """
        create_id = "elixirDraft"
        state = self._object_state("Email", self.mail_account_id, [CORE, MAIL])
        attempts = len(self.retry_delays) + 1
        for attempt in range(attempts):
            try:
                responses = self.call(
                    [
                        [
                            "Email/set",
                            {
                                "accountId": self.mail_account_id,
                                "ifInState": state,
                                "create": {create_id: email_obj},
                            },
                            "create",
                        ]
                    ],
                    using=[CORE, MAIL],
                )
                payload = responses[0][1]
                not_created = (payload.get("notCreated") or {}).get(create_id)
                if not_created:
                    if not_created.get("type") == "alreadyExists" and not_created.get("existingId"):
                        existing = self._get_email(str(not_created["existingId"]))
                        if existing:
                            return existing
                    raise JMAPError(f"Email draft was not created: {not_created}")
                created = (payload.get("created") or {}).get(create_id) or {}
                if not created.get("id"):
                    raise JMAPError(f"Email/set returned no created draft: {payload!r}")
                return created
            except (requests.Timeout, requests.ConnectionError, JMAPError) as exc:
                retryable = self._is_transient(exc) or (
                    isinstance(exc, JMAPMethodError) and exc.error_type == _STATE_MISMATCH
                )
                if retryable:
                    existing = self._find_email_by_message_id(message_id)
                    if existing:
                        log.warning("Fastmail draft create recovered from %s", type(exc).__name__)
                        return existing
                if not retryable or attempt + 1 >= attempts:
                    raise
                if isinstance(exc, JMAPMethodError) and exc.error_type == _STATE_MISMATCH:
                    state = self._object_state("Email", self.mail_account_id, [CORE, MAIL])
                self._wait_before_retry("email-create", attempt, attempts, exc)
        raise AssertionError("unreachable")

    def _submit_draft(self, email_id: str, rcpt_to: list[dict[str, Any]]) -> dict[str, Any]:
        """Submit a draft with state-guarded retries and sent-folder reconciliation."""
        submit_id = "elixirSend"
        state = self._object_state(
            "EmailSubmission", self.submission_account_id, [CORE, SUBMISSION]
        )
        attempts = len(self.retry_delays) + 1
        for attempt in range(attempts):
            try:
                responses = self.call(
                    [
                        [
                            "EmailSubmission/set",
                            {
                                "accountId": self.submission_account_id,
                                "ifInState": state,
                                "create": {
                                    submit_id: {
                                        "identityId": self.identity_id,
                                        "emailId": email_id,
                                        "envelope": {
                                            "mailFrom": {
                                                "email": EMAIL_ADDRESS,
                                                "parameters": None,
                                            },
                                            "rcptTo": rcpt_to,
                                        },
                                    }
                                },
                                "onSuccessUpdateEmail": {
                                    f"#{submit_id}": {
                                        f"mailboxIds/{self.folders.drafts}": None,
                                        f"mailboxIds/{self.folders.sent_elixir}": True,
                                        "keywords/$draft": None,
                                        "keywords/$seen": True,
                                    }
                                },
                            },
                            "submit",
                        ]
                    ],
                    using=[CORE, MAIL, SUBMISSION],
                )
                payload = responses[0][1]
                not_created = (payload.get("notCreated") or {}).get(submit_id)
                if not_created:
                    raise JMAPError(f"Email was not submitted: {not_created}")
                created = (payload.get("created") or {}).get(submit_id) or {}
                if not created.get("id"):
                    raise JMAPError(f"EmailSubmission/set returned no submission: {payload!r}")
                return {**created, "recovered": False}
            except (requests.Timeout, requests.ConnectionError, JMAPError) as exc:
                retryable = self._is_transient(exc) or (
                    isinstance(exc, JMAPMethodError) and exc.error_type == _STATE_MISMATCH
                )
                if retryable and self._email_is_sent(email_id):
                    log.warning("Fastmail submission recovered from %s", type(exc).__name__)
                    return {"id": None, "recovered": True}
                if not retryable or attempt + 1 >= attempts:
                    raise
                if isinstance(exc, JMAPMethodError) and exc.error_type == _STATE_MISMATCH:
                    state = self._object_state(
                        "EmailSubmission", self.submission_account_id, [CORE, SUBMISSION]
                    )
                self._wait_before_retry("email-submit", attempt, attempts, exc)
        raise AssertionError("unreachable")

    def send_email(
        self,
        *,
        to: list[str] | str,
        subject: str,
        body: str,
        html_body: str | None = None,
        cc: list[str] | str | None = None,
        bcc: list[str] | str | None = None,
        in_reply_to: str | None = None,
        references: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create the message as a draft and submit it; on success move it out of
        Drafts into Sent/Elixir. Mirrors Oliver's Email/set + EmailSubmission/set."""
        recipients = _addresses(to)
        if not recipients:
            raise JMAPError("At least one recipient is required")
        cc_recipients = _addresses(cc)
        bcc_recipients = _addresses(bcc)
        if html_body is None:
            html_body = email_render.text_to_html(body)
        if html_body:
            body_structure = {
                "type": "multipart/alternative",
                "subParts": [
                    {"type": "text/plain", "partId": "text"},
                    {"type": "text/html", "partId": "html"},
                ],
            }
            body_values = {
                "text": {"value": body, "isTruncated": False},
                "html": {"value": html_body, "isTruncated": False},
            }
        else:
            body_structure = {"type": "text/plain", "partId": "text"}
            body_values = {"text": {"value": body, "isTruncated": False}}
        email_obj: dict[str, Any] = {
            "mailboxIds": {self.folders.drafts: True},
            "keywords": {"$draft": True, "$seen": True},
            "from": [{"name": EMAIL_FROM_NAME, "email": EMAIL_ADDRESS}],
            "to": recipients,
            "subject": subject,
            "bodyStructure": body_structure,
            "bodyValues": body_values,
        }
        domain = EMAIL_ADDRESS.rsplit("@", 1)[-1] or "poapkings.com"
        message_id = f"elixir.{uuid.uuid4().hex}@{domain}"
        email_obj["header:Message-ID:asMessageIds"] = [message_id]
        if cc_recipients:
            email_obj["cc"] = cc_recipients
        if bcc_recipients:
            email_obj["bcc"] = bcc_recipients
        refs = [r for r in (references or []) if r]
        if in_reply_to:
            email_obj["inReplyTo"] = [in_reply_to]
            if in_reply_to not in refs:
                refs.append(in_reply_to)
        if refs:
            email_obj["references"] = refs[-20:]

        rcpt_to = [
            {"email": r["email"], "parameters": None}
            for r in recipients + cc_recipients + bcc_recipients
        ]
        created_email = self._create_draft(email_obj, message_id)
        created_submission = self._submit_draft(str(created_email["id"]), rcpt_to)
        return {
            "emailId": created_email.get("id"),
            "threadId": created_email.get("threadId"),
            "submissionId": created_submission.get("id"),
            "messageId": message_id,
            "recovered": bool(created_submission.get("recovered")),
            "to": [r["email"] for r in recipients],
            "cc": [r["email"] for r in cc_recipients],
            "bccCount": len(bcc_recipients),
            "subject": subject,
        }


_client: JMAPClient | None = None


def client() -> JMAPClient:
    global _client
    if _client is None:
        _client = JMAPClient()
    return _client


def send_email(**kwargs) -> dict[str, Any]:
    return client().send_email(**kwargs)


def _addresses(value: list[str] | str | None) -> list[dict[str, str | None]]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    parsed = getaddresses(raw)
    out = []
    seen = set()
    for name, email in parsed:
        email = email.strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        out.append({"name": name or None, "email": email})
    return out
