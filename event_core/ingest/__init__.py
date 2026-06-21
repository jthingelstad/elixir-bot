"""Ingest — parse CR API payloads into aggregate commands.

One code path used by both backfill (replaying archived raw_api_payloads) and
live ingest (later). Pure functions where possible so they are trivially testable.
"""
