"""Fastmail retries must recover transient failures without duplicate sends."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from agent.mail import email_jmap


def _client(*, retry_delays=(0.0, 0.0)) -> email_jmap.JMAPClient:
    client = email_jmap.JMAPClient(token="test-token", retry_delays=retry_delays)
    client.__dict__["session"] = {
        "apiUrl": "https://jmap.test/api",
        "primaryAccounts": {
            email_jmap.MAIL: "mail-account",
            email_jmap.SUBMISSION: "submission-account",
        },
    }
    client.__dict__["folders"] = email_jmap.MailFolders(
        sent_elixir="sent-mailbox", drafts="drafts-mailbox"
    )
    client.__dict__["identity_id"] = "identity-id"
    return client


def _response(payload: dict, *, status_code: int = 200):
    response = Mock()
    response.status_code = status_code
    response.text = ""
    response.json.return_value = payload
    return response


def test_safe_jmap_read_retries_a_read_timeout(monkeypatch):
    client = _client(retry_delays=(0.0,))
    responses = [
        requests.ReadTimeout("Fastmail was slow"),
        _response({"methodResponses": [["Mailbox/get", {"list": []}, "read"]]}),
    ]

    def fake_post(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    post = Mock(side_effect=fake_post)
    monkeypatch.setattr(email_jmap.requests, "post", post)

    result = client.call(
        [["Mailbox/get", {"accountId": "mail-account"}, "read"]],
        retry_transient=True,
        operation="test-read",
    )

    assert result[0][0] == "Mailbox/get"
    assert post.call_count == 2


def test_mutating_jmap_call_never_blindly_retries(monkeypatch):
    client = _client(retry_delays=(0.0, 0.0))
    post = Mock(side_effect=requests.ReadTimeout("response was lost"))
    monkeypatch.setattr(email_jmap.requests, "post", post)

    with pytest.raises(requests.ReadTimeout):
        client.call([["Email/set", {"accountId": "mail-account"}, "write"]])

    assert post.call_count == 1


def test_draft_timeout_recovers_committed_create_by_message_id(monkeypatch):
    client = _client()
    message_id = "elixir.retry-test@poapkings.com"
    set_states = []
    query_count = 0

    def fake_call(method_calls, **kwargs):
        nonlocal query_count
        method, args, call_id = method_calls[0]
        if method == "Email/get" and call_id == "state":
            return [[method, {"state": "email-state-1", "list": []}, call_id]]
        if method == "Email/set":
            set_states.append(args["ifInState"])
            if len(set_states) == 1:
                raise requests.ReadTimeout("response was lost")
            raise email_jmap.JMAPMethodError(call_id, {"type": "stateMismatch"})
        if method == "Email/query":
            query_count += 1
            ids = [] if query_count == 1 else ["draft-id"]
            return [[method, {"ids": ids}, call_id]]
        if method == "Email/get" and call_id == "email":
            return [
                [
                    method,
                    {
                        "list": [
                            {
                                "id": "draft-id",
                                "threadId": "thread-id",
                                "mailboxIds": {"drafts-mailbox": True},
                                "messageId": [message_id],
                            }
                        ]
                    },
                    call_id,
                ]
            ]
        raise AssertionError(method_calls)

    monkeypatch.setattr(client, "call", fake_call)

    created = client._create_draft({"header:Message-ID:asMessageIds": [message_id]}, message_id)

    assert created["id"] == "draft-id"
    assert set_states == ["email-state-1", "email-state-1"]


def test_submission_timeout_retries_same_state_and_then_succeeds(monkeypatch):
    client = _client()
    set_states = []
    sent_checks = 0

    def fake_call(method_calls, **kwargs):
        nonlocal sent_checks
        method, args, call_id = method_calls[0]
        if method == "EmailSubmission/get":
            return [[method, {"state": "submission-state-1", "list": []}, call_id]]
        if method == "EmailSubmission/set":
            set_states.append(args["ifInState"])
            if len(set_states) == 1:
                raise requests.ReadTimeout("response was lost")
            return [
                [
                    method,
                    {"created": {"elixirSend": {"id": "submission-id"}}},
                    call_id,
                ]
            ]
        if method == "Email/get":
            sent_checks += 1
            return [
                [
                    method,
                    {
                        "list": [
                            {
                                "id": "draft-id",
                                "mailboxIds": {"drafts-mailbox": True},
                            }
                        ]
                    },
                    call_id,
                ]
            ]
        raise AssertionError(method_calls)

    monkeypatch.setattr(client, "call", fake_call)

    result = client._submit_draft("draft-id", [{"email": "member@example.com"}])

    assert result == {"id": "submission-id", "recovered": False}
    assert set_states == ["submission-state-1", "submission-state-1"]
    assert sent_checks == 1


def test_submission_timeout_recovers_success_without_a_second_send(monkeypatch):
    client = _client()
    set_states = []
    sent_checks = 0

    def fake_call(method_calls, **kwargs):
        nonlocal sent_checks
        method, args, call_id = method_calls[0]
        if method == "EmailSubmission/get":
            return [[method, {"state": "submission-state-1", "list": []}, call_id]]
        if method == "EmailSubmission/set":
            set_states.append(args["ifInState"])
            if len(set_states) == 1:
                raise requests.ReadTimeout("response was lost")
            raise email_jmap.JMAPMethodError(call_id, {"type": "stateMismatch"})
        if method == "Email/get":
            sent_checks += 1
            mailbox = "drafts-mailbox" if sent_checks == 1 else "sent-mailbox"
            return [
                [
                    method,
                    {"list": [{"id": "draft-id", "mailboxIds": {mailbox: True}}]},
                    call_id,
                ]
            ]
        raise AssertionError(method_calls)

    monkeypatch.setattr(client, "call", fake_call)

    result = client._submit_draft("draft-id", [{"email": "member@example.com"}])

    assert result == {"id": None, "recovered": True}
    assert set_states == ["submission-state-1", "submission-state-1"]
    assert sent_checks == 2


def test_submission_state_mismatch_refreshes_guard_before_retry(monkeypatch):
    client = _client()
    states = iter(["submission-state-1", "submission-state-2"])
    set_states = []

    def fake_call(method_calls, **kwargs):
        method, args, call_id = method_calls[0]
        if method == "EmailSubmission/get":
            return [[method, {"state": next(states), "list": []}, call_id]]
        if method == "EmailSubmission/set":
            set_states.append(args["ifInState"])
            if len(set_states) == 1:
                raise email_jmap.JMAPMethodError(call_id, {"type": "stateMismatch"})
            return [
                [
                    method,
                    {"created": {"elixirSend": {"id": "submission-id"}}},
                    call_id,
                ]
            ]
        if method == "Email/get":
            return [
                [
                    method,
                    {
                        "list": [
                            {
                                "id": "draft-id",
                                "mailboxIds": {"drafts-mailbox": True},
                            }
                        ]
                    },
                    call_id,
                ]
            ]
        raise AssertionError(method_calls)

    monkeypatch.setattr(client, "call", fake_call)

    result = client._submit_draft("draft-id", [{"email": "member@example.com"}])

    assert result == {"id": "submission-id", "recovered": False}
    assert set_states == ["submission-state-1", "submission-state-2"]


def test_send_email_uses_a_unique_message_id_and_two_phase_delivery(monkeypatch):
    client = _client()
    captured = {}

    def fake_create(email_obj, message_id):
        captured["email_obj"] = email_obj
        captured["message_id"] = message_id
        return {"id": "draft-id", "threadId": "thread-id"}

    def fake_submit(email_id, recipients):
        captured["submitted_email_id"] = email_id
        captured["recipients"] = recipients
        return {"id": "submission-id", "recovered": False}

    monkeypatch.setattr(client, "_create_draft", fake_create)
    monkeypatch.setattr(client, "_submit_draft", fake_submit)

    result = client.send_email(
        to="elixir@poapkings.com",
        bcc=["alpha@example.com", "bravo@example.com"],
        subject="Weekly Clan Recap",
        body="A good week.",
    )

    assert captured["email_obj"]["header:Message-ID:asMessageIds"] == [captured["message_id"]]
    assert captured["submitted_email_id"] == "draft-id"
    assert [row["email"] for row in captured["recipients"]] == [
        "elixir@poapkings.com",
        "alpha@example.com",
        "bravo@example.com",
    ]
    assert result["submissionId"] == "submission-id"
    assert result["bccCount"] == 2
