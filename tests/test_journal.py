"""Tests for journal.py — rolling messages for poapkings.com."""

import json
import os
import tempfile

import journal


def test_save_and_load_messages():
    """Save a message and load it back."""
    with tempfile.TemporaryDirectory() as tmp:
        msg = journal.save_message(tmp, "POAP KINGS on top! 🧪")
        assert msg["text"] == "POAP KINGS on top! 🧪"
        assert "date" in msg

        messages = journal.load_messages(tmp)
        assert len(messages) == 1
        assert messages[0]["text"] == "POAP KINGS on top! 🧪"


def test_rolling_limit():
    """Only the last MAX_MESSAGES are kept."""
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(7):
            journal.save_message(tmp, f"Message {i}")

        messages = journal.load_messages(tmp)
        assert len(messages) == journal.MAX_MESSAGES
        # Oldest messages are trimmed
        assert messages[0]["text"] == "Message 2"
        assert messages[-1]["text"] == "Message 6"


def test_load_empty_repo():
    """Returns empty list when file doesn't exist."""
    with tempfile.TemporaryDirectory() as tmp:
        messages = journal.load_messages(tmp)
        assert messages == []


def test_json_structure():
    """File has the expected simple structure."""
    with tempfile.TemporaryDirectory() as tmp:
        journal.save_message(tmp, "Hello world")
        path = os.path.join(tmp, journal.ELIXIR_JSON_FILENAME)
        with open(path) as f:
            data = json.load(f)
        assert "messages" in data
        assert len(data["messages"]) == 1
        assert set(data["messages"][0].keys()) == {"text", "date"}


def test_creates_directories():
    """Creates parent directories if they don't exist."""
    with tempfile.TemporaryDirectory() as tmp:
        journal.save_message(tmp, "test")
        path = os.path.join(tmp, journal.ELIXIR_JSON_FILENAME)
        assert os.path.exists(path)
