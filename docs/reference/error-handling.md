# Error Handling and Logging Policy

Elixir is intentionally fail-soft at external seams, but fail-soft must not mean
fail-silent. Handle an exception according to the work that was lost.

**Where a failure goes:** `logs/elixir-error.log` — ERROR and up, with
tracebacks, written by `runtime/logging_setup.py`. It is deliberately small
(~6 lines/day) so an operator can read the whole file, and reading it is the
Operations Manager's job (`AGENT-TEAM/error-watch.md`). What lands at ERROR is
therefore a budget, not a dumping ground: every line there is a line someone
must account for.

This used to be a `runtime_incidents` table. It recorded 0 rows in 25 days while
the log held 159 real errors, and the daily health check that read it reported
"all clear" through every one — so the ledger and the check were both retired
2026-07-28 (schema v20). The log was always the record.

## Categories

| Category | Examples | Required behavior | Log at ERROR? |
|---|---|---|---|
| Expected parsing/probing | Optional JSON field, timestamp variant, missing scheduler job | Catch the narrow expected exception and return/skip with a clear fallback. | No. |
| User or tool boundary | Bad tool arguments, unavailable optional enrichment, rejected model response | Return a bounded error or deterministic fallback; log below ERROR when diagnosis would otherwise be lost. | Usually no. |
| Abandoned runtime work | A Discord card could not refresh, tick history did not persist, a delivered action failed to update its case | Preserve the primary operation when safe, then `log.exception("<component> failed: k=%s", v)` so the traceback is captured. | Yes. |
| Scheduled activity failure | Poll, report, or maintenance job failed | Update `runtime_job_status` or re-raise to the activity guard; log at ERROR when the lost work is not otherwise represented. | Status or ERROR; both for high-value loss. |
| Observability failure | The logging setup or a diagnostic UI itself failed | Never crash the path it observes; log on the handler you still have and return. | Yes, once, non-recursively. |

## Rules

1. Catch the narrowest exception whose meaning is understood. `Exception` is
   reserved for real process boundaries, injected/plugin code, or a fallback
   that must protect a primary operation.
2. Never use a bare `except` or catch `BaseException`; shutdown and interrupt
   signals must propagate.
3. A broad handler must do something explicit: re-raise, update job/status
   state, log, report to the caller, or apply a named fallback.
   `except Exception: pass` is prohibited.
4. Do not log expected user mistakes, optional missing data, or normal scheduler
   idempotency at ERROR. That turns the error log into noise, and a noisy error
   log is one nobody reads — which is exactly how the failures it exists to
   catch stay invisible.
5. Log with the module's own logger, named for its dotted path
   (`logging.getLogger("engine.tick")`, `"elixir.storage.player"`). Prefix the
   message with the stable dotted component and `<component> failed: `, so the
   line groups cleanly: `log.exception("threads.lock failed: thread_id=%s", tid)`.
6. Pass context as %-style lazy args, never f-strings — entity keys such as
   player tag, action ID, message ID, or activity key belong in the args, not
   interpolated into the format string. Use `log.exception` when an exception is
   in scope (it captures the traceback), `log.error` otherwise.

## Enforcement

`scripts/check_exception_hygiene.py` scans production packages. It rejects bare
and `BaseException` catches, broad pass-only handlers, and unhandled broad
catches. Its per-file baseline makes the existing migration debt explicit: any
broad catch added, removed, or moved requires a deliberate baseline update.

The baseline is not permission to keep broad catches forever. Narrowing a catch
should lower it. Tests and one-shot scripts are
outside this production-runtime budget.
