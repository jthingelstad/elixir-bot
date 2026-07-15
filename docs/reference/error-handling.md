# Error Handling and Incident Policy

Elixir is intentionally fail-soft at external seams, but fail-soft must not mean
fail-silent. Handle an exception according to the work that was lost.

## Categories

| Category | Examples | Required behavior | Incident? |
|---|---|---|---|
| Expected parsing/probing | Optional JSON field, timestamp variant, missing scheduler job | Catch the narrow expected exception and return/skip with a clear fallback. | No. |
| User or tool boundary | Bad tool arguments, unavailable optional enrichment, rejected model response | Return a bounded error or deterministic fallback; log when diagnosis would otherwise be lost. | Usually no. |
| Abandoned runtime work | A Discord card could not refresh, tick history did not persist, a delivered action failed to update its case | Preserve the primary operation when safe, log, and record `runtime_incidents` with stable component/context. | Yes. |
| Scheduled activity failure | Poll, report, or maintenance job failed | Update `runtime_job_status` or re-raise to the activity guard; record an incident when work is otherwise not represented. | Status or incident; both for high-value loss. |
| Observability failure | Incident recording or diagnostic UI itself failed | Never crash or recursively record; log the failure and return. | No recursive incident. |

## Rules

1. Catch the narrowest exception whose meaning is understood. `Exception` is
   reserved for real process boundaries, injected/plugin code, or a fallback
   that must protect a primary operation.
2. Never use a bare `except` or catch `BaseException`; shutdown and interrupt
   signals must propagate.
3. A broad handler must do something explicit: re-raise, record an incident,
   update job/status state, log, report to the caller, or apply a named fallback.
   `except Exception: pass` is prohibited.
4. Do not record expected user mistakes, optional missing data, or normal
   scheduler idempotency as incidents. That would turn the ledger into noise.
5. Incident component names are stable dotted identifiers. Include entity keys
   such as player tag, action ID, message ID, or activity key in `context`, not
   in the component name.
6. When using a borrowed transaction, prefer recording with the same connection
   only if the transaction remains valid. Otherwise log and use the incident
   helper's independent connection; the helper never raises.

## Enforcement

`scripts/check_exception_hygiene.py` scans production packages. It rejects bare
and `BaseException` catches, broad pass-only handlers, and unhandled broad
catches. Its per-file baseline makes the existing migration debt explicit: any
broad catch added, removed, or moved requires a deliberate baseline update.

The baseline is not permission to keep broad catches forever. Narrowing a catch
should lower it. Tests and one-shot scripts are
outside this production-runtime budget.
