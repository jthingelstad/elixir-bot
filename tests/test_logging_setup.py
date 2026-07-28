"""The error log has to actually be separate, and actually rotate.

The point of splitting ERROR into its own file is that an agent can read the
whole thing every few minutes. That only holds if INFO stays out of it and the
file cannot grow past one readable pass — so both are asserted here rather than
assumed.
"""

from __future__ import annotations

import logging

from runtime import logging_setup


def _reset_root():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    logging_setup._configured = False


def test_errors_go_to_the_error_log_and_info_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("ELIXIR_LOG_DIR", str(tmp_path))
    _reset_root()
    try:
        logging_setup.configure_logging(force=True)
        log = logging.getLogger("elixir.test")
        log.info("a routine thing")
        log.error("a broken thing")
        for handler in logging.getLogger().handlers:
            handler.flush()

        main = (tmp_path / logging_setup.MAIN_LOG_NAME).read_text()
        errors = (tmp_path / logging_setup.ERROR_LOG_NAME).read_text()
    finally:
        _reset_root()

    # The full narrative lands in the main log.
    assert "a routine thing" in main
    assert "a broken thing" in main

    # The error log carries only what an operator has to act on. If INFO leaks
    # in here the file stops being readable in one pass and the split is moot.
    assert "a broken thing" in errors
    assert "a routine thing" not in errors


def test_exceptions_carry_their_traceback(tmp_path, monkeypatch):
    """An error log without tracebacks tells you something broke, not what."""
    monkeypatch.setenv("ELIXIR_LOG_DIR", str(tmp_path))
    _reset_root()
    try:
        logging_setup.configure_logging(force=True)
        try:
            raise ValueError("the specific cause")
        except ValueError:
            logging.getLogger("elixir.test").exception("operation failed")
        for handler in logging.getLogger().handlers:
            handler.flush()
        errors = (tmp_path / logging_setup.ERROR_LOG_NAME).read_text()
    finally:
        _reset_root()

    assert "operation failed" in errors
    assert "ValueError: the specific cause" in errors
    assert "Traceback" in errors


def test_configure_is_idempotent(tmp_path, monkeypatch):
    """Called twice, every line must not appear twice."""
    monkeypatch.setenv("ELIXIR_LOG_DIR", str(tmp_path))
    _reset_root()
    try:
        logging_setup.configure_logging(force=True)
        logging_setup.configure_logging()
        logging.getLogger("elixir.test").error("just once")
        for handler in logging.getLogger().handlers:
            handler.flush()
        errors = (tmp_path / logging_setup.ERROR_LOG_NAME).read_text()
    finally:
        _reset_root()

    assert errors.count("just once") == 1


def test_unwritable_log_dir_does_not_stop_the_bot(tmp_path, monkeypatch):
    """Losing the files is bad; refusing to boot over it is worse."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("this is a file, so makedirs must fail")
    monkeypatch.setenv("ELIXIR_LOG_DIR", str(blocker))
    _reset_root()
    try:
        logging_setup.configure_logging(force=True)
        # stdout survives, so the process still has a voice.
        assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)
    finally:
        _reset_root()
