"""Release notes (ported from Oliver): git material → named, first-person
announcement → GitHub release + Discord #announcements. Mirrors Oliver's
test shape; no live sends, no LLM calls (stubbed at _create_chat_completion)."""

from types import SimpleNamespace

from agent import release_notes as rn

MATERIAL = {
    "window": "since v4.8 (2026-04-16)",
    "days": None,
    "since_ref": "v4.8",
    "count": 3,
    "truncated": False,
    "merges": "",
    "commits": "### abc123 2026-07-04 Add a shiny thing\nbody detail here\n",
    "changed_docs": ["docs/reference/v5.1/README.md"],
    "releases_head": "# Elixir Releases\n\n## v4.8 — Trophy Hall",
}


def _resp(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def test_git_output_reads_history():
    assert rn._git(["log", "--oneline", "-1"]).strip()


def test_git_output_empty_on_bad_command():
    assert rn._git(["log", "--this-flag-does-not-exist"]) == ""


def test_latest_release_tag_is_versioned():
    # Environment-independent: a tagless checkout (shallow clone) must not fail
    # the suite — None is valid. CI now fetches tags; when present, assert shape.
    tag = rn.latest_release_tag()
    if tag is None:
        import pytest
        pytest.skip("no v* tags in this checkout")
    assert tag.startswith("v")


def test_release_history_parses_releases_md():
    hist = rn.release_history()
    assert {"version": "v4.8", "name": "Trophy Hall"} in hist
    # Newest first — v4.8 precedes v4.1 in the file.
    versions = [h["version"] for h in hist]
    assert versions.index("v4.8") < versions.index("v4.1")


def test_prompt_embeds_material_and_contract():
    p = rn.release_notes_prompt(MATERIAL)
    for section in ("## The story", "## Features", "## Release Notes"):
        assert section in p
    assert "first person" in p.lower()
    assert "<subject>" in p and "<notes>" in p
    assert "a shiny thing" in p            # material embedded, not described
    assert "Trophy Hall" in p              # RELEASES.md context included
    assert "OPEN:" in p and "framing sentence" in p
    assert "CLOSE:" in p and "sign-off" in p
    assert "terse changelog" in p
    assert "POAP KINGS" in p               # re-voiced for the clan


def test_prompt_carries_release_name():
    p = rn.release_notes_prompt({**MATERIAL, "release_name": "Blazing Balloon"})
    assert 'christened "Blazing Balloon"' in p
    assert "Clash Royale card" in p


def test_prompt_notes_truncation():
    p = rn.release_notes_prompt({**MATERIAL, "count": 200, "truncated": True})
    assert "200 commits" in p


def test_extract_subject_and_notes():
    out = "<subject>Five new tricks</subject><notes>## The story\nI grew.</notes>"
    assert rn._extract_subject(out) == "Five new tricks"
    assert rn._extract_notes(out) == "## The story\nI grew."
    # Tolerates a missing close tag: subject takes only the first line.
    assert rn._extract_subject("<subject>Solo line\n<notes>body") == "Solo line"


def test_release_notes_draft_stubbed(monkeypatch):
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: dict(MATERIAL))
    monkeypatch.setattr(rn, "coin_release_name", lambda m: "Golden Goblin")
    import agent.core as core
    monkeypatch.setattr(core, "_create_chat_completion",
                        lambda **kw: _resp("<subject>Shiny</subject><notes>I can now shine.</notes>"))
    draft = rn.release_notes_draft(since_ref="v4.8")
    assert draft == {"subject": "Shiny", "body": "I can now shine.",
                     "window": MATERIAL["window"], "release_name": "Golden Goblin"}


def test_draft_none_when_no_changes(monkeypatch):
    monkeypatch.setattr(rn, "recent_changes", lambda **kw: {**MATERIAL, "count": 0})
    assert rn.release_notes_draft(days=1) is None


def test_coin_name_rejects_malformed(monkeypatch):
    import agent.core as core
    monkeypatch.setattr(rn, "_card_names", lambda: ["Balloon", "Golem"])
    monkeypatch.setattr(core, "_create_chat_completion",
                        lambda **kw: _resp("Blazing Balloon\nextra line"))
    assert rn.coin_release_name(dict(MATERIAL)) == ""   # multi-line → nameless
    monkeypatch.setattr(core, "_create_chat_completion",
                        lambda **kw: _resp('"Gilded Golem"'))
    assert rn.coin_release_name(dict(MATERIAL)) == "Gilded Golem"  # quotes stripped


def test_coin_name_failure_tolerant(monkeypatch):
    monkeypatch.setattr(rn, "_card_names", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert rn.coin_release_name(dict(MATERIAL)) == ""


def test_announcement_messages_chunk_and_link():
    msgs = rn.announcement_messages(subject="Shiny", body="line\n\n" + ("x " * 3000),
                                    release_url="https://github.com/r/r/releases/tag/v5.1",
                                    version="v5.1", name="Golden Goblin")
    assert len(msgs) >= 2
    assert msgs[0].startswith('**v5.1 "Golden Goblin"** — Shiny')
    assert "github.com" in msgs[-1]
    assert all(len(m) <= 2000 for m in msgs)


def test_announcement_messages_nameless():
    msgs = rn.announcement_messages(subject="S", body="b", release_url=None,
                                    version="v5.1", name="")
    assert msgs[0].startswith("**v5.1** — S")
    assert "GitHub" not in msgs[0]
