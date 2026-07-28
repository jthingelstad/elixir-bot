"""Process logging: a rotating main log, and a separate error log to watch.

Elixir used to log INFO+ to stdout and nothing else. launchd captured that into
one flat `elixir-v5.log` that grew without bound, so the only way to ask "what
is failing?" was to grep 25 days and ~15,000 lines of mostly-INFO for the ~6
errors a day that matter. In practice nobody did, and real failures sat unread:
a #thinking permission outage ran for hours, and the daily health check
reported nothing because it consulted an incident table that had never recorded
a single row while the log held 159 errors.

So: two files, plain `logging.handlers`, no bespoke machinery.

* ``logs/elixir.log`` — INFO and up. The full narrative, rotated so it cannot
  eat the disk.
* ``logs/elixir-error.log`` — ERROR and up, with tracebacks. Small enough
  (~6 lines a day) that an agent can read the whole file every few minutes and
  act on what it finds.

The split is the point. An error log you can read in full is a log that gets
read; the value is in what it leaves out.

stdout keeps its handler so launchd still captures anything that never reaches
logging at all — interpreter tracebacks on a hard crash, third-party prints.
That file is the catch-all, not the record.

Configured from the process entrypoint rather than at import, so importing
`runtime.app` in a test never creates a file.
"""

from __future__ import annotations

import logging
import logging.handlers
import os

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Rotation is sized so the error log stays readable in one pass and the main log
# keeps roughly a fortnight at current volume (~600 KB/day).
_MAIN_MAX_BYTES = 10 * 1024 * 1024
_MAIN_BACKUPS = 5
_ERROR_MAX_BYTES = 2 * 1024 * 1024
_ERROR_BACKUPS = 5

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAIN_LOG_NAME = "elixir.log"
ERROR_LOG_NAME = "elixir-error.log"

_configured = False


def log_dir() -> str:
    """Where the log files live. ELIXIR_LOG_DIR overrides for a sandbox."""
    return os.getenv("ELIXIR_LOG_DIR") or os.path.join(_REPO_ROOT, "logs")


def error_log_path() -> str:
    """The file an operator or agent should watch."""
    return os.path.join(log_dir(), ERROR_LOG_NAME)


def main_log_path() -> str:
    return os.path.join(log_dir(), MAIN_LOG_NAME)


def _rotating(path: str, level: int, max_bytes: int, backups: int) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT))
    return handler


def configure_logging(*, force: bool = False) -> str:
    """Attach the stdout, main-file, and error-file handlers to the root logger.

    Idempotent: calling it twice will not double every line. Returns the log
    directory in use.

    If the log directory cannot be created or opened we keep the stdout handler
    and carry on — losing the files is bad, but refusing to boot the bot over it
    is worse, and the failure is reported on the handler we still have.
    """
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        return log_dir()

    level = getattr(logging, (os.getenv("ELIXIR_LOG_LEVEL") or "INFO").upper(), logging.INFO)
    root.setLevel(level)

    if force:
        for existing in list(root.handlers):
            root.removeHandler(existing)

    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(stream)

    directory = log_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        root.addHandler(_rotating(main_log_path(), level, _MAIN_MAX_BYTES, _MAIN_BACKUPS))
        root.addHandler(
            _rotating(error_log_path(), logging.ERROR, _ERROR_MAX_BYTES, _ERROR_BACKUPS)
        )
    except OSError:
        logging.getLogger("elixir.logging").error(
            "file logging unavailable at %s — stdout only, so the error log an "
            "operator watches will not exist",
            directory,
            exc_info=True,
        )

    _configured = True
    return directory
