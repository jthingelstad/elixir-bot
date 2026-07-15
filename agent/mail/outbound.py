"""Elixir's outbound-email convenience wrapper (ported from Oliver's outbound.py).

Adds Elixir's sign-off to the body/HTML and delegates to the JMAP client. Callers
that want the raw client can use email_jmap.send_email directly.
"""

from __future__ import annotations

from typing import Any

from agent.mail import email_jmap, email_render

_TEXT_SIG = "— Elixir · POAP KINGS"
_HTML_SIG = (
    '<div class="elixir-sig"><p>— <strong>Elixir</strong> · POAP KINGS</p></div>'
)


def send(
    *,
    to: list[str] | str,
    subject: str,
    body: str,
    sign: bool = True,
    cc: list[str] | str | None = None,
    bcc: list[str] | str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> dict[str, Any]:
    """Send an email as Elixir. When sign=True the text body gets a plain sign-off and
    the HTML alternative gets Elixir's styled signature footer. Use bcc for broadcasts
    so recipients don't see each other's addresses."""
    text_body = f"{body.rstrip()}\n\n{_TEXT_SIG}" if sign else body
    html_body = (
        email_render.text_to_html(body, signature_html=_HTML_SIG if sign else None)
        if email_jmap.HTML_ENABLED
        else None
    )
    return email_jmap.send_email(
        to=to,
        subject=subject,
        body=text_body,
        html_body=html_body,
        cc=cc,
        bcc=bcc,
        in_reply_to=in_reply_to,
        references=references,
    )


def enabled() -> bool:
    return email_jmap.enabled()
