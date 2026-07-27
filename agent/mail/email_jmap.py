"""Fastmail JMAP mail client for Elixir (ported from Oliver's agent/mail/email_jmap.py).

The Fastmail account is shared across several agent identities (Otto, Thingy,
Oliver, Elixir); Elixir sends as ELIXIR_EMAIL_ADDRESS and stores sent mail in
Sent/Elixir. This is the send path only — inbound reading lives elsewhere if it
is ever needed. Config comes straight from the environment.
"""

from __future__ import annotations

import logging
import os
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
HTML_ENABLED = os.getenv("ELIXIR_EMAIL_HTML_ENABLED", "1") != "0"


class JMAPError(RuntimeError):
    """Raised for missing config, malformed JMAP state, or server-side errors."""


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
    ) -> None:
        self.token = token or JMAP_TOKEN
        self.session_url = session_url or JMAP_SESSION_URL
        self.timeout = timeout
        if not self.token:
            raise JMAPError("FASTMAIL_JMAP_TOKEN is not configured")

    @cached_property
    def session(self) -> dict[str, Any]:
        r = requests.get(
            self.session_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout,
        )
        self._raise_for_response(r)
        return r.json()

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
        self, method_calls: list[list[Any]], *, using: list[str] | None = None
    ) -> list[list[Any]]:
        body = {"using": using or [CORE, MAIL, SUBMISSION], "methodCalls": method_calls}
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
                raise JMAPError(f"JMAP call {call_id} failed: {payload}")
        return data["methodResponses"]

    @staticmethod
    def _raise_for_response(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as e:
            text = response.text[:500]
            raise JMAPError(f"Fastmail HTTP {response.status_code}: {text}") from e

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
        create_id = "elixirDraft"
        submit_id = "elixirSend"
        if html_body is None and HTML_ENABLED:
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
        responses = self.call(
            [
                [
                    "Email/set",
                    {
                        "accountId": self.mail_account_id,
                        "create": {create_id: email_obj},
                    },
                    "create",
                ],
                [
                    "EmailSubmission/set",
                    {
                        "accountId": self.submission_account_id,
                        "create": {
                            submit_id: {
                                "identityId": self.identity_id,
                                "emailId": f"#{create_id}",
                                "envelope": {
                                    "mailFrom": {
                                        "email": EMAIL_ADDRESS,
                                        "parameters": None,
                                    },
                                    "rcptTo": rcpt_to,
                                },
                            },
                        },
                        "onSuccessUpdateEmail": {
                            f"#{submit_id}": {
                                f"mailboxIds/{self.folders.drafts}": None,
                                f"mailboxIds/{self.folders.sent_elixir}": True,
                                "keywords/$draft": None,
                                "keywords/$seen": True,
                            },
                        },
                    },
                    "submit",
                ],
            ]
        )
        create_payload = responses[0][1]
        submit_payload = responses[1][1]
        if create_payload.get("notCreated"):
            raise JMAPError(f"Email draft was not created: {create_payload['notCreated']}")
        if submit_payload.get("notCreated"):
            raise JMAPError(f"Email was not submitted: {submit_payload['notCreated']}")
        created_email = (create_payload.get("created") or {}).get(create_id) or {}
        created_submission = (submit_payload.get("created") or {}).get(submit_id) or {}
        return {
            "emailId": created_email.get("id"),
            "threadId": created_email.get("threadId"),
            "submissionId": created_submission.get("id"),
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
