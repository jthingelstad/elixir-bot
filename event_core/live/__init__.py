"""Live runtime wiring (Stage 4).

Connects the running bot to the v5 core: route fetched CR payloads through the
shared ingest path, advance the Followers incrementally (resume from tracked
positions — not a full rebuild), and consume CommunicationIntents to Discord.

Built behind seams; nothing here touches the live service until the Stage 6
go-live (see docs/archive/event-core-v5/event-core-v5-cutover-runbook.md).
"""
