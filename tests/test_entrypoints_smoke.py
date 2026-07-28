"""Entrypoint smoke tests — the highest-ROI guard against the silent
import/NameError bug class (confidence plan Phase 1).

The bugs this catches, from live incidents:
  - `can_post_leader_action` referenced in a job without an import → EVERY
    engine leader-action card silently failed to post (2026-07-05).
  - `_ensure_role_action_clan_chat_copy` imported CLASH_COPY_MAX_LENGTH from the
    wrong module → the whole card-copy path died lazily on first use.
  - `auto_withdraw_leader_actions` defined but never committed → `main` could
    not import at all.

These never fire in unit tests because the units are individually fine — the
failure is a name that only resolves (or doesn't) when the wiring runs. Two
techniques cover it without real I/O:

1. STATIC: disassemble every function in every runtime module and assert every
   LOAD_GLOBAL name resolves in that module's globals or builtins. Catches an
   unimported/misspelled/uncommitted global name WITHOUT invoking (the
   can_post_leader_action / auto_withdraw class).
2. DYNAMIC: minimally invoke the entrypoints whose failure hides in a lazy
   `from X import Y` executed only on call (the CLASH_COPY_MAX_LENGTH class) —
   compose asks per intent type, leader-action card builds per action type.

Plus registry-consistency checks: every scheduled job resolves to a callable,
every advertised tool has an executor.
"""

from __future__ import annotations

import ast
import builtins
import dis
import importlib
import inspect
import pkgutil
import textwrap
import types

_BUILTINS = set(dir(builtins))

# Packages whose runtime code must have clean name resolution. scripts/ is
# excluded (standalone entrypoints with load_dotenv side effects).
_SCAN_PACKAGES = ("runtime", "engine", "agent", "storage")


# ------------------------------------------------------------ static analysis


def _iter_code_objects(code):
    """A code object and every nested code object (closures, comprehensions)."""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _iter_code_objects(const)


def _undefined_globals_for_code(code, func_globals):
    """LOAD_GLOBAL names in a code object (and its nested codes) that resolve in
    NEITHER the given globals NOR builtins — a guaranteed NameError."""
    missing = set()
    for co in _iter_code_objects(code):
        for instr in dis.get_instructions(co):
            if instr.opname == "LOAD_GLOBAL":
                name = instr.argval
                if name not in func_globals and name not in _BUILTINS:
                    missing.add(name)
    return missing


def _scan_function(func, findings, seen_codes):
    """Scan a function AND its decorator wrapper chain. Each layer is resolved
    against ITS OWN __globals__ — critical because functools.wraps copies
    __module__ onto a wrapper while the wrapper's code lives (and its globals
    resolve) in the DECORATOR's module."""
    current = func
    while isinstance(current, types.FunctionType):
        code = current.__code__
        if id(code) not in seen_codes:
            seen_codes.add(id(code))
            missing = _undefined_globals_for_code(code, current.__globals__)
            if missing:
                findings.append(
                    f"{current.__module__}.{current.__qualname__}: undefined {sorted(missing)}"
                )
        current = getattr(current, "__wrapped__", None)


def _discover_modules():
    names = []
    for pkg_name in _SCAN_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        names.append(pkg_name)
        for info in pkgutil.walk_packages(pkg.__path__, prefix=pkg_name + "."):
            if info.name.rsplit(".", 1)[-1].startswith("_") and info.name.endswith("__main__"):
                continue
            names.append(info.name)
    return sorted(set(names))


def test_all_runtime_modules_import():
    """Module-level import integrity — catches an uncommitted symbol / bad
    top-level import (the auto_withdraw_leader_actions class)."""
    failures = []
    for name in _discover_modules():
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — the failure IS the finding
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, "modules failed to import:\n" + "\n".join(failures)


def test_no_unresolved_global_names():
    """Every LOAD_GLOBAL in every runtime function resolves. This is the
    can_post_leader_action guard: a function referencing an unimported name."""
    findings = []
    seen_codes = set()
    for name in _discover_modules():
        try:
            module = importlib.import_module(name)
        except Exception:
            continue  # import failures are reported by the test above
        for obj in list(module.__dict__.values()):
            if isinstance(obj, types.FunctionType) and obj.__module__ == name:
                _scan_function(obj, findings, seen_codes)
    assert not findings, (
        "functions reference names that resolve in neither module globals nor "
        "builtins (a NameError waiting to fire):\n" + "\n".join(findings)
    )


# --------------------------------------------------------- registry integrity


def test_registered_job_functions_resolve():
    """Every scheduled activity's job_function is a real callable on the runtime
    module (the getattr the scheduler does at register time)."""
    import runtime.app as app
    from runtime.activities import list_registered_activities

    missing = []
    for activity in list_registered_activities():
        fn = getattr(app, activity.job_function, None)
        if not callable(fn):
            missing.append(f"{activity.activity_key} -> {activity.job_function}")
    assert not missing, "job_function not a callable on runtime.app:\n" + "\n".join(missing)


def test_every_advertised_tool_has_an_executor():
    """Every tool exposed to any workflow can actually be executed — no
    advertised-but-unroutable tool (they'd fail only when the LLM calls them)."""
    from agent import tool_exec
    from agent.workflow_registry import ALL_TOOLS

    advertised = {tool["name"] for tool in ALL_TOOLS}
    dispatch_tree = ast.parse(textwrap.dedent(inspect.getsource(tool_exec._execute_tool)))
    dispatched = {
        node.comparators[0].value
        for node in ast.walk(dispatch_tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "name"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and isinstance(node.comparators[0].value, str)
    }
    assert advertised == tool_exec.TOOL_EXECUTOR_NAMES == dispatched


def test_leader_action_card_builds_for_every_type(engine_conn):
    """Every leader-action type builds its embed + view + (for role/kick types)
    its clan-chat copy without raising — guards the card path where the lazy
    CLASH_COPY_MAX_LENGTH import silently killed posting."""
    import asyncio

    from runtime import leader_action_ui as ui
    from runtime.clan_chat_copy import ROLE_ACTION_TYPES, role_action_clan_chat_copy

    for action_type in ui.ACTION_SPECS:
        action = {
            "action_id": 1,
            "action_type": action_type,
            "objective": "x",
            "prompt_text": "do the thing",
            "rationale": "because reasons",
            "target_player_tag": "#A",
            "target_player_name": "Alice",
            "status": "proposed",
        }
        ui.build_leader_action_embed(action)  # embed build
        ui.leader_action_view_for(action)  # interactive view build
        if action_type in ROLE_ACTION_TYPES:
            # deterministic clan-chat copy (the fallback the gate falls back to)
            assert role_action_clan_chat_copy(
                action_type=action_type,
                target_player_name="Alice",
                rationale="because reasons",
            )

    # the runtime enrichment path itself (async, deterministic fallback)
    import runtime.app as app

    action = {
        "action_id": 1,
        "action_type": "promotion_recommendation",
        "objective": "x",
        "prompt_text": "x",
        "rationale": "Ultimate Champion.",
        "target_player_tag": "#A",
        "target_player_name": "Alice",
        "status": "proposed",
        "copy_current_text": None,
        "copy_original_text": None,
    }

    async def _boom(*a, **k):
        raise RuntimeError("no LLM in tests")

    import runtime.clan_chat_copy as ccc

    orig = ccc.generate_clan_chat_copy
    ccc.generate_clan_chat_copy = _boom
    try:
        out = asyncio.run(app._ensure_role_action_clan_chat_copy(action))
    finally:
        ccc.generate_clan_chat_copy = orig
    assert out.get("copy_current_text")  # deterministic fallback attached
