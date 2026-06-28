"""Timestamp SQL helpers for mixed legacy projection formats."""
from __future__ import annotations


def cr_comparable_expr(column: str) -> str:
    """SQLite expression that renders ISO or CR timestamps as `YYYYMMDDThhmmss`.

    The v5 projection store historically mixed ISO UTC values and compact Clash
    Royale timestamps. Both become lexically comparable once reduced to this
    fixed-width UTC form.
    """
    return (
        "CASE "
        f"WHEN {column} IS NULL THEN NULL "
        f"WHEN substr({column}, 5, 1) = '-' THEN "
        f"substr({column}, 1, 4) || substr({column}, 6, 2) || substr({column}, 9, 2) "
        f"|| 'T' || substr({column}, 12, 2) || substr({column}, 15, 2) || substr({column}, 18, 2) "
        f"ELSE substr({column}, 1, 15) "
        "END"
    )
