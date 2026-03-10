import db
from memory_reasoner import format_memory_for_response, package_prompt_context, summarize_member_memories
from memory_store import (
    MemoryValidationError,
    archive_memory,
    attach_evidence_ref,
    attach_tags,
    create_memory,
    get_memory,
    list_memories,
    search_memories,
    soft_delete_memory,
    update_memory,
    upsert_embedding,
)
from runtime.admin import _build_memory_report


def test_memory_schema_tables_exist_and_separate_from_authoritative_facts():
    conn = db.get_connection(":memory:")
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "memory_facts" in tables
        assert "clan_memories" in tables

        memory = create_memory(
            body="Leader noted stronger attendance this week.",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader:jamie",
            conn=conn,
        )
        facts = db.get_memory_facts("member", "999", conn=conn)
        assert facts == []
        assert get_memory(memory["memory_id"], conn=conn)["body"].startswith("Leader noted")
    finally:
        conn.close()


def test_provenance_rules_and_retrieval_payload():
    conn = db.get_connection(":memory:")
    try:
        item = create_memory(
            body="Pattern suggests slight disengagement.",
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.7,
            created_by="elixir",
            scope="leadership",
            conn=conn,
        )
        assert item["source_type"] == "elixir_inference"
        assert item["is_inference"] == 1
        assert item["confidence"] == 0.7

        try:
            create_memory(
                body="bad",
                source_type="elixir_inference",
                is_inference=True,
                confidence=1.0,
                created_by="elixir",
                conn=conn,
            )
            assert False, "expected validation error"
        except MemoryValidationError:
            pass
    finally:
        conn.close()


def test_permissions_filters_and_lifecycle_controls():
    conn = db.get_connection(":memory:")
    try:
        public = create_memory(
            body="Public note",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            scope="public",
            conn=conn,
        )
        leadership = create_memory(
            body="Leadership-only note",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            scope="leadership",
            conn=conn,
        )
        internal = create_memory(
            body="Internal system tuning",
            source_type="system",
            is_inference=False,
            confidence=1.0,
            created_by="system",
            scope="system_internal",
            conn=conn,
        )

        public_view = list_memories(viewer_scope="public", conn=conn)
        assert [m["memory_id"] for m in public_view] == [public["memory_id"]]

        leader_view = list_memories(viewer_scope="leadership", conn=conn)
        leader_ids = {m["memory_id"] for m in leader_view}
        assert public["memory_id"] in leader_ids
        assert leadership["memory_id"] in leader_ids
        assert internal["memory_id"] not in leader_ids

        system_view = list_memories(viewer_scope="system_internal", conn=conn)
        system_ids = {m["memory_id"] for m in system_view}
        assert internal["memory_id"] in system_ids

        archive_memory(leadership["memory_id"], actor="leader", conn=conn)
        assert get_memory(leadership["memory_id"], viewer_scope="leadership", conn=conn) is None

        soft_delete_memory(public["memory_id"], actor="leader", conn=conn)
        assert get_memory(public["memory_id"], viewer_scope="system_internal", conn=conn) is None
    finally:
        conn.close()


def test_structured_filters_tags_evidence_and_audit_versions():
    conn = db.get_connection(":memory:")
    try:
        member_id = db._ensure_member(conn, "#AAA111", name="Alpha")
        item = create_memory(
            body="Promotion after consistent war week.",
            summary="Promotion candidate",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            member_id=member_id,
            member_tag="#AAA111",
            role="elder",
            war_season_id="2026-s02",
            war_week_id="2026-s02-w03",
            event_type="promotion",
            event_id="prom-123",
            conn=conn,
        )
        attach_tags(item["memory_id"], ["promotion", "war"], actor="leader", conn=conn)
        attach_evidence_ref(
            item["memory_id"],
            evidence_type="war_week",
            evidence_ref="2026-s02-w03",
            evidence_label="War week 3",
            actor="leader",
            conn=conn,
        )
        updated = update_memory(item["memory_id"], actor="leader", summary="Updated summary", conn=conn)
        assert updated["summary"] == "Updated summary"
        assert "promotion" in updated["tags"]
        assert updated["evidence_refs"][0]["evidence_ref"] == "2026-s02-w03"

        rows = list_memories(
            viewer_scope="leadership",
            filters={
                "member_id": member_id,
                "member_tag": "AAA111",
                "role": "elder",
                "war_season_id": "2026-s02",
                "war_week_id": "2026-s02-w03",
                "event_type": "promotion",
                "event_id": "prom-123",
                "scope": "leadership",
                "source_type": "leader_note",
                "is_inference": False,
                "status": "active",
            },
            conn=conn,
        )
        assert len(rows) == 1

        versions = conn.execute(
            "SELECT COUNT(*) AS c FROM clan_memory_versions WHERE memory_id = ?",
            (item["memory_id"],),
        ).fetchone()["c"]
        audits = conn.execute(
            "SELECT COUNT(*) AS c FROM clan_memory_audit_log WHERE memory_id = ?",
            (item["memory_id"],),
        ).fetchone()["c"]
        assert versions >= 1
        assert audits >= 4
    finally:
        conn.close()


def test_hybrid_search_rrf_and_fts_only_degraded_mode():
    conn = db.get_connection(":memory:")
    try:
        a = create_memory(
            body="Bravo showed war consistency and deck discipline.",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            conn=conn,
        )
        b = create_memory(
            body="Charlie might need recognition for support role effort.",
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.8,
            created_by="elixir",
            conn=conn,
        )

        upsert_embedding(a["memory_id"], [1.0, 0.0, 0.0], conn=conn)
        upsert_embedding(b["memory_id"], [0.0, 1.0, 0.0], conn=conn)

        def embed_query(_):
            return [1.0, 0.0, 0.0]

        hybrid = search_memories("war consistency", embed_query=embed_query, conn=conn)
        assert hybrid
        assert hybrid[0].memory["memory_id"] == a["memory_id"]
        assert "lexical_rank" in hybrid[0].components

        fts_only = search_memories("recognition", embed_query=None, conn=conn)
        assert fts_only
        assert any(r.memory["memory_id"] == b["memory_id"] for r in fts_only)
    finally:
        conn.close()


def test_summarization_and_safe_phrasing_helpers():
    conn = db.get_connection(":memory:")
    try:
        member_id = db._ensure_member(conn, "#BBB222", name="Bravo")
        leader = create_memory(
            body="Leadership encouraged Bravo to keep consistency.",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            member_id=member_id,
            conn=conn,
        )
        infer = create_memory(
            body="Bravo may need motivation after two weaker cycles.",
            source_type="elixir_inference",
            is_inference=True,
            confidence=0.45,
            created_by="elixir",
            member_id=member_id,
            conn=conn,
        )

        packet = package_prompt_context(facts=[{"kind": "authoritative_fact"}], memories=[leader, infer])
        assert len(packet["facts"]) == 1
        assert len(packet["leadership_memories"]) == 1
        assert len(packet["assistant_inferences"]) == 1

        summary = summarize_member_memories(member_id, conn=conn)
        assert summary.leadership_notes
        assert summary.assistant_inferences
        assert summary.open_questions

        leader_text = format_memory_for_response(leader)
        infer_text = format_memory_for_response(infer)
        assert leader_text.startswith("Leadership noted")
        assert "inferred" in infer_text and "confidence" in infer_text
    finally:
        conn.close()


def test_build_memory_report_shows_member_conversation_and_contextual_memory():
    conn = db.get_connection(":memory:")
    try:
        db.snapshot_members(
            [{"tag": "#ABC123", "name": "King Levy", "role": "member"}],
            conn=conn,
        )
        db.link_discord_user_to_member(
            "1474760692992180429",
            "#ABC123",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )
        db.save_message(
            "leader:1474760692992180429",
            "user",
            "How am I doing lately?",
            discord_user_id="1474760692992180429",
            username="jamie",
            display_name="King Levy",
            conn=conn,
        )
        create_memory(
            body="Leader noted steadier war usage from King Levy.",
            summary="Steadier war usage",
            source_type="leader_note",
            is_inference=False,
            confidence=1.0,
            created_by="leader",
            member_tag="#ABC123",
            conn=conn,
        )

        report = _build_memory_report(member_query="King Levy", limit=3, conn=conn)

        assert "**Elixir Memory**" in report
        assert "King Levy" in report
        assert "`#ABC123`" in report
        assert "Conversation memory:" in report
        assert "`last_user_summary`" in report
        assert "Recent contextual memories for King Levy" in report
        assert "Steadier war usage" in report
    finally:
        conn.close()


def test_build_memory_report_search_can_include_system_internal():
    conn = db.get_connection(":memory:")
    try:
        create_memory(
            body="Internal tuning note for memory index degradation.",
            summary="Memory index degradation",
            source_type="system",
            is_inference=False,
            confidence=1.0,
            created_by="system",
            scope="system_internal",
            conn=conn,
        )

        report = _build_memory_report(
            query="index degradation",
            include_system_internal=True,
            limit=3,
            conn=conn,
        )

        assert "- View: `system_internal`" in report
        assert "Contextual memory search for `index degradation`:" in report
        assert "Memory index degradation" in report
    finally:
        conn.close()


def test_build_memory_report_global_view_shows_conversation_memory_counts():
    conn = db.get_connection(":memory:")
    try:
        db.save_message(
            "leader:user123",
            "user",
            "Who should we promote?",
            discord_user_id="user123",
            username="jamie",
            display_name="Jamie",
            conn=conn,
        )

        report = _build_memory_report(limit=2, conn=conn)

        assert "- Conversation store: 1 facts | 1 episodes | 0 channel states" in report
        assert "Recent conversation memory:" in report
        assert "Fact `discord_user:user123` `last_user_summary`" in report
        assert "Episode `discord_user:user123` `user` importance 1" in report
    finally:
        conn.close()
