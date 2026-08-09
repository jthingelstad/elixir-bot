"""Shell-safe registered activity runner.

This module is intentionally small: it resolves activities through the canonical
runtime.activities registry, enforces manual-trigger policy, and can run under a
short-lived Discord REST channel lookup for shell operators.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import discord

from runtime.activities import get_activity, resolve_activity
from runtime.scheduled_catchup import wrap_scheduled_activity


class ActivityRunError(RuntimeError):
    """Base exception for operator-facing activity run failures."""


class UnknownActivityError(ActivityRunError):
    """Raised when an activity key or alias is not registered."""


class ManualActivityNotAllowed(ActivityRunError):
    """Raised when the registry forbids manual runs for an activity."""


@dataclass(frozen=True)
class ActivityRunResult:
    activity_key: str
    job_function: str
    result: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "activity_key": self.activity_key,
            "job_function": self.job_function,
            "result": self.result,
        }


class _ChannelLookup:
    def __init__(self, channels: dict[int, Any]):
        self._channels = channels

    def get_channel(self, channel_id: int | str):
        try:
            return self._channels.get(int(channel_id))
        except TypeError, ValueError:
            return None


def _channel_ids_for_lookup(channel_configs: list[dict], thinking_channel_id: object) -> set[int]:
    """Return every Discord channel a shell-triggered activity may need.

    ``#thinking`` is intentionally outside ``prompts/DISCORD.md`` because it is
    an operator diagnostic surface, but the awareness loop posts to it through
    the same runtime bot lookup. Omitting it makes a manual awareness run look
    healthy while silently dropping its diagnostic stream.
    """
    channel_ids = {int(config["id"]) for config in channel_configs}
    if thinking_channel_id is not None:
        channel_ids.add(int(thinking_channel_id))
    return channel_ids


def _validate_manual_activity(activity_key: str):
    activity = get_activity(activity_key)
    if activity is None:
        raise UnknownActivityError(f"unknown activity: {activity_key}")
    if not activity.manual_trigger_allowed:
        raise ManualActivityNotAllowed(
            f"activity {activity.activity_key} does not allow manual runs"
        )
    return activity


async def run_activity_once(activity_key: str, *, runtime_module: Any) -> ActivityRunResult:
    activity = _validate_manual_activity(activity_key)
    resolved = resolve_activity(activity.activity_key, runtime_module)
    job_callable = wrap_scheduled_activity(resolved)
    result = job_callable()
    if inspect.isawaitable(result):
        result = await result
    return ActivityRunResult(
        activity_key=resolved["activity_key"],
        job_function=resolved["job_function"],
        result=result,
    )


async def _build_rest_channel_lookup(
    runtime_module: Any,
) -> tuple[discord.Client, _ChannelLookup]:
    import prompts

    token = getattr(runtime_module, "TOKEN", None)
    if not token:
        raise ActivityRunError("DISCORD_TOKEN is not configured")

    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)

        channels: dict[int, Any] = {}
        channel_ids = _channel_ids_for_lookup(
            prompts.discord_channel_configs(),
            getattr(runtime_module, "THINKING_CHANNEL_ID", None),
        )
        for channel_id in channel_ids:
            # A deleted/unreachable channel must not crash the whole shell runner
            # — skip it (the startup audit already reports reachability).
            try:
                channels[channel_id] = await client.fetch_channel(channel_id)
            except Exception:
                import logging

                # ERROR: the activity runs but its output goes nowhere. A
                # skipped channel is indistinguishable from a quiet one.
                logging.getLogger("elixir").exception(
                    "activity_runner: channel %s unreachable; skipping", channel_id
                )
        return client, _ChannelLookup(channels)
    except Exception:
        await client.close()
        raise


@asynccontextmanager
async def shell_activity_runtime(runtime_module: Any):
    """Patch the runtime's bot lookup to a REST-only channel lookup.

    The shell runner should not start a second Discord gateway session. Logging
    in and using fetch/send over REST gives scheduled jobs a normal channel.send
    surface without competing with the launchd-managed bot process.
    """

    client, channel_lookup = await _build_rest_channel_lookup(runtime_module)
    try:
        with patch.object(runtime_module, "bot", channel_lookup):
            yield
    finally:
        await client.close()


async def run_shell_activity(activity_key: str) -> ActivityRunResult:
    _validate_manual_activity(activity_key)
    import elixir

    async with shell_activity_runtime(elixir):
        return await run_activity_once(activity_key, runtime_module=elixir)


def _print_json(payload: dict[str, Any], *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), file=stream or sys.stdout)


async def _run_cli(args: argparse.Namespace) -> int:
    try:
        result = await run_shell_activity(args.activity)
    except UnknownActivityError as exc:
        _print_json(
            {"ok": False, "error": str(exc), "error_type": "unknown_activity"},
            stream=sys.stderr,
        )
        return 2
    except ManualActivityNotAllowed as exc:
        _print_json(
            {"ok": False, "error": str(exc), "error_type": "manual_not_allowed"},
            stream=sys.stderr,
        )
        return 3
    except Exception as exc:
        _print_json(
            {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            stream=sys.stderr,
        )
        return 1

    _print_json({"ok": True, **result.as_dict()})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a registered Elixir activity once.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one manual-triggerable activity.")
    run_parser.add_argument("activity", help="Activity key or registry alias.")

    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run_cli(args))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
