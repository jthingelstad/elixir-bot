"""clan_daily_metrics projection — daily rollup of clan-level state.

A DAILY ROLLUP keyed by (clan_tag, metric_date) where metric_date is an
America/Chicago calendar day. The rollup value for a day is the LAST
ClanStateObserved within that Chicago day (matching legacy's per-day upsert: the
last snapshot of the day wins).

UTC-only data layer (§7): events carry a UTC observed_at; the Chicago-day
conversion happens here at projection time, in event_core.timeutil (the one
isolated TZ place). Replaying events in notification order makes the
last-write-per-day deterministic — events arrive in observation order, so the
final upsert for a (tag, day) is the latest observation.

Deferred fields (roster-lifecycle concern, NOT clan-level state): joins_today,
leaves_today, net_member_change. Those come from clan_memberships join/left date
diffs, which belong to a roster join/leave aggregate — out of this slice. They
are created with their schema defaults (0) and explicitly not parity-checked.
"""
from __future__ import annotations

from event_core.domain.clan import CLAN_STATE_FIELDS
from event_core.projections.runner import ProjectionRunner
from event_core.timeutil import chicago_day_for_utc

# Column SQL types. avg is REAL; clan_name is TEXT; the rest INTEGER.
_REAL = {"avg_member_trophies"}
_TEXT = {"clan_name"}


def _col_type(col: str) -> str:
    if col in _TEXT:
        return "TEXT"
    if col in _REAL:
        return "REAL"
    return "INTEGER"


class ClanDailyMetrics(ProjectionRunner):
    name = "clan_daily_metrics_proj"
    aggregate_name = "Clan"

    def setup(self) -> None:
        cols = ",\n            ".join(
            f"{c} {_col_type(c)}" for c in CLAN_STATE_FIELDS
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS clan_daily_metrics_proj (
                clan_tag    TEXT NOT NULL,
                metric_date TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                {cols},
                -- Deferred roster-lifecycle fields (schema parity; not computed here).
                joins_today      INTEGER NOT NULL DEFAULT 0,
                leaves_today     INTEGER NOT NULL DEFAULT 0,
                net_member_change INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (clan_tag, metric_date)
            )
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS clan_daily_metrics_proj")
        self.conn.commit()
        super().reset()
        self.setup()

    def handle(self, event, notification) -> None:
        cls = type(event).__name__
        if cls == "Registered":
            # Remember tag per aggregate id so ClanStateObserved (which carries no
            # tag) can resolve it. Stored in a tiny side table in this same conn.
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS _clan_agg_tag (aggregate_id TEXT PRIMARY KEY, clan_tag TEXT)"
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO _clan_agg_tag(aggregate_id, clan_tag) VALUES(?,?)",
                (str(event.originator_id), event.clan_tag),
            )
            return
        if cls != "ClanStateObserved":
            return

        agg_id = str(event.originator_id)
        row = self.conn.execute(
            "SELECT clan_tag FROM _clan_agg_tag WHERE aggregate_id=?", (agg_id,)
        ).fetchone()
        clan_tag = row["clan_tag"] if row else None
        if not clan_tag:
            return

        metric_date = chicago_day_for_utc(event.observed_at)
        if metric_date is None:
            return

        obs = dict(event.observation)
        cols = list(CLAN_STATE_FIELDS)
        present = [c for c in cols if c in obs]
        # Upsert the day; LAST observation within the Chicago day wins (events are
        # replayed in observation order, so later notifications overwrite earlier).
        insert_cols = ["clan_tag", "metric_date", "observed_at"] + present
        placeholders = ",".join("?" for _ in insert_cols)
        updates = ", ".join(
            [f"{c}=excluded.{c}" for c in ["observed_at"] + present]
        )
        vals = [clan_tag, metric_date, event.observed_at] + [obs[c] for c in present]
        self.conn.execute(
            f"INSERT INTO clan_daily_metrics_proj({','.join(insert_cols)}) "
            f"VALUES({placeholders}) "
            f"ON CONFLICT(clan_tag, metric_date) DO UPDATE SET {updates}",
            vals,
        )
