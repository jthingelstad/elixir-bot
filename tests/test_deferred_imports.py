"""Every function-local import must actually resolve.

A deferred import resolves at CALL time, not at import time. So a wrong module
path or a renamed symbol inside a function body is invisible to the interpreter
until the moment that line runs — often on a path that only fires in
production. That is not hypothetical here: `_ensure_role_action_clan_chat_copy`
once imported `CLASH_COPY_MAX_LENGTH` from the wrong module and the card-copy
path died lazily on first use, and separately a function-local
`from storage.cases import ...` survived a module rename and broke the engine
tick's MANAGE step twelve times overnight.

`tests/test_entrypoints_smoke.py` reaches for this class and structurally
cannot catch it: it scans `LOAD_GLOBAL` bytecode, and a deferred
`from X import Y` compiles to `IMPORT_NAME`/`IMPORT_FROM` + `STORE_FAST` —
never a global load.

This test parses every function body, finds each first-party import, and
checks that the target module exists and actually defines every name pulled
from it. It resolves the `db` facade's lazily-exported names through its
declared registry rather than importing them, so it stays fast and has no
import-order side effects.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ("engine", "runtime", "storage", "capabilities", "agent", "db", "memory_store")
ROOT_MODULES = {"prompts", "cr_api", "cr_knowledge", "elixir", "elixir_agent"}
SKIP_DIRS = {".venv", "tests", "node_modules", "scratchpad", "docs", "scripts"}


def _is_first_party(module: str) -> bool:
    head = module.split(".")[0]
    return head in PACKAGES or head in ROOT_MODULES


def _sources():
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _deferred_imports():
    """(file, lineno, module, [names]) for every import inside a function body."""
    for path in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # hygiene: a parse failure is ruff's job, not this test's
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    for alias in sub.names:
                        if _is_first_party(alias.name):
                            yield path, sub.lineno, alias.name, []
                elif isinstance(sub, ast.ImportFrom):
                    if sub.level or not sub.module or not _is_first_party(sub.module):
                        continue
                    yield path, sub.lineno, sub.module, [a.name for a in sub.names]


def _db_facade_names() -> set[str]:
    """`db` exports most of its surface lazily via __getattr__; read the registry."""
    import db

    return set(db._CORE_EXPORTS) | set(db._FACADE_EXPORTS)


CASES = sorted(
    {(str(p.relative_to(ROOT)), ln, mod, tuple(names)) for p, ln, mod, names in _deferred_imports()}
)


def test_there_are_deferred_imports_to_check():
    """Guards the guard: an AST change that silently matches nothing must fail."""
    assert len(CASES) > 50, f"only found {len(CASES)} deferred imports — the walker is broken"


@pytest.mark.parametrize("path,lineno,module,names", CASES, ids=lambda v: str(v)[:40])
def test_a_deferred_import_resolves(path, lineno, module, names):
    try:
        target = importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - the failure we exist to catch
        pytest.fail(f"{path}:{lineno} defers `import {module}`, which does not import: {exc}")

    if not names:
        return

    available = set(dir(target))
    if module == "db":
        # `db` re-exports most of its surface through a lazy __getattr__, so dir()
        # shows only what has already been touched. Read the declared registry.
        available |= _db_facade_names()

    missing = []
    for name in names:
        if name in available:
            continue
        # `from package import submodule` is legal and dir() will not show the
        # submodule until something imports it.
        try:
            importlib.import_module(f"{module}.{name}")
        except ImportError:
            missing.append(name)
    assert not missing, (
        f"{path}:{lineno} defers `from {module} import {', '.join(names)}` but "
        f"{module} does not define {missing}. A deferred import resolves at CALL "
        f"time, so this would raise in production on whatever path reaches it."
    )
