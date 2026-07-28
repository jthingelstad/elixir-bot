"""Regression coverage for the repository's deliberate compatibility facades."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import agent.tools
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
    assert len(entries) == 318
    assert _digest(entries) == "37ee4866d9fb69d43ba5cec858e2598d7b1e7469aca5e7a7d3171ed3847b4b02"
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


def test_tool_facade_public_surface_is_reviewed():
    assert len(agent.tools.__all__) == 21
    assert _digest(agent.tools.__all__) == (
        "980882faf82e67c3fe5085d443f305f8e709b3c5817b984d2a96aa3eac15032a"
    )
    assert agent.tools.execute_tool is agent.tools._execute_tool
    assert all(hasattr(agent.tools, name) for name in agent.tools.__all__)


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
    for relative in ("db/__init__.py", "agent/tools.py", "storage/war.py"):
        assert "__export_public" not in (ROOT / relative).read_text()
