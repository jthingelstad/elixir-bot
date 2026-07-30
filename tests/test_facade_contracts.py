"""Regression coverage for the repository's deliberate compatibility facades."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import db
import storage.war
from storage import war_analytics, war_members, war_status

ROOT = Path(__file__).resolve().parents[1]


def _digest(values) -> str:
    payload = "\n".join(sorted(values)).encode()
    return hashlib.sha256(payload).hexdigest()


def test_db_facade_public_surface_is_reviewed():
    entries = [
        *(f"{name}:db" for name in db._CORE_EXPORTS),
        *(f"{name}:{module}" for name, module in db._FACADE_EXPORTS.items()),
    ]
    assert len(entries) == 246
    # Updated 2026-07-30: +3. DECIDED_VIA_BUTTON / DECIDED_VIA_REACTION so the UI
    # can tell decide_leader_action how a decision was entered (so removing a ✅
    # reaction can no longer take back a ✅ BUTTON press), and
    # get_leader_action_by_message so the reaction handler can re-render a card
    # whose decision was refused instead of ignoring the reaction in silence.
    # (Earlier that day: one rename, get_weekly_digest_summary ->
    # get_weekly_recap_summary, count unchanged at 243.)
    assert _digest(entries) == "8554bc6835b9c9cf339ea999a9ad8827589bb736933a85b8000f68a29a9cd8e8"
    assert db._CORE_EXPORTS.isdisjoint(db._FACADE_EXPORTS)
    assert db.__all__ == sorted(db._CORE_EXPORTS | set(db._FACADE_EXPORTS))


def test_db_facade_rejects_colliding_declarations():
    with pytest.raises(RuntimeError, match="declared by both"):
        db._build_facade_exports(
            {
                "storage.first": ("same_name",),
                "storage.second": ("same_name",),
            }
        )


def test_db_facade_declared_sources_resolve_exactly():
    script = (
        "import importlib, db\n"
        "for name, module_name in db._FACADE_EXPORTS.items():\n"
        "    module = importlib.import_module(module_name)\n"
        "    assert getattr(db, name) is getattr(module, name), name\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


@pytest.mark.parametrize(
    "script",
    [
        "import storage.roster as source; import db; "
        "assert db.resolve_member is source.resolve_member",
        "import db; import storage.roster as source; "
        "assert db.resolve_member is source.resolve_member",
    ],
)
def test_db_facade_is_stable_across_import_order(script):
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True)


def test_war_facade_is_exact_union_of_read_domains():
    source_names = [
        *war_status.__all__,
        *war_members.__all__,
        *war_analytics.__all__,
    ]
    assert len(source_names) == len(set(source_names))
    assert storage.war.__all__ == sorted(source_names)
    assert len(storage.war.__all__) == 56
    assert _digest(storage.war.__all__) == (
        "e8d415776054b90991b7903869aec1ba1ecdac3a038cb6082b75d9b31f1653b4"
    )


def test_facades_do_not_copy_module_namespaces():
    for relative in ("db/__init__.py", "storage/war.py"):
        assert "__export_public" not in (ROOT / relative).read_text()
