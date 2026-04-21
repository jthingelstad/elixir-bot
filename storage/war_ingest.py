from db import (
    _canon_tag,
    _ensure_member,
    _json_or_none,
    _store_raw_payload,
    _tag_key,
    _utcnow,
    managed_connection,
)
from storage.war_calendar import (
    PERIODS_PER_WEEK,
    phase_day_number,
    period_offset,
    resolve_phase,
    war_day_key,
)


def _get_latest_logged_race(conn):
    return conn.execute(
        "SELECT season_id, section_index FROM war_races ORDER BY season_id DESC, section_index DESC, war_race_id DESC LIMIT 1"
    ).fetchone()


def _infer_current_season_id_from_live_state(payload, latest_logged_race):
    live_season_id = (payload or {}).get("seasonId")
    if live_season_id is not None:
        return live_season_id
    if not latest_logged_race:
        return None
    live_section_index = (payload or {}).get("sectionIndex")
    logged_section_index = latest_logged_race["section_index"]
    if (
        live_section_index is not None
        and logged_section_index is not None
        and live_section_index < logged_section_index
    ):
        return latest_logged_race["season_id"] + 1
    return latest_logged_race["season_id"]


def _period_offset(period_index):
    return period_offset(period_index)


def _normalize_period_type(period_type):
    from storage.war_calendar import normalize_period_type

    return normalize_period_type(period_type)


def _resolve_phase(period_type, period_index):
    return resolve_phase(period_type, period_index)


def _phase_day_number(phase, period_index):
    return phase_day_number(phase, period_index)


def _war_day_key(season_id, section_index, period_index, observed_at=None):
    return war_day_key(season_id, section_index, period_index, observed_at)


def _infer_period_section_index(period_index, current_section_index, current_period_index):
    if period_index is None:
        return current_section_index
    if (current_period_index or 0) >= PERIODS_PER_WEEK or period_index >= PERIODS_PER_WEEK:
        return period_index // PERIODS_PER_WEEK
    return current_section_index


def _upsert_period_logs(conn, observed_at, war_data, season_id):
    current_section_index = (war_data or {}).get("sectionIndex")
    current_period_index = (war_data or {}).get("periodIndex")
    clan_name_by_tag = {}
    for clan in ((war_data or {}).get("clans") or []):
        canon_tag = _canon_tag(clan.get("tag"))
        if canon_tag:
            clan_name_by_tag[canon_tag] = clan.get("name")
    primary_clan = (war_data or {}).get("clan") or {}
    primary_clan_tag = _canon_tag(primary_clan.get("tag"))
    if primary_clan_tag:
        clan_name_by_tag[primary_clan_tag] = primary_clan.get("name")

    for period_log in ((war_data or {}).get("periodLogs") or []):
        period_index = period_log.get("periodIndex")
        section_index = _infer_period_section_index(
            period_index,
            current_section_index=current_section_index,
            current_period_index=current_period_index,
        )
        period_offset = _period_offset(period_index)
        for item in (period_log.get("items") or []):
            clan = item.get("clan") or {}
            clan_tag = _canon_tag(clan.get("tag"))
            if not clan_tag:
                continue
            conn.execute(
                "INSERT INTO war_period_clan_status (season_id, section_index, period_index, period_offset, clan_tag, clan_name, points_earned, progress_start_of_day, progress_end_of_day, end_of_day_rank, progress_earned, num_defenses_remaining, progress_earned_from_defenses, observed_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(season_id, section_index, period_index, clan_tag) DO UPDATE SET "
                "period_offset = excluded.period_offset, clan_name = excluded.clan_name, points_earned = excluded.points_earned, "
                "progress_start_of_day = excluded.progress_start_of_day, progress_end_of_day = excluded.progress_end_of_day, "
                "end_of_day_rank = excluded.end_of_day_rank, progress_earned = excluded.progress_earned, "
                "num_defenses_remaining = excluded.num_defenses_remaining, "
                "progress_earned_from_defenses = excluded.progress_earned_from_defenses, observed_at = excluded.observed_at, raw_json = excluded.raw_json",
                (
                    season_id,
                    section_index,
                    period_index,
                    period_offset,
                    clan_tag,
                    clan_name_by_tag.get(clan_tag),
                    item.get("pointsEarned"),
                    item.get("progressStartOfDay"),
                    item.get("progressEndOfDay"),
                    item.get("endOfDayRank"),
                    item.get("progressEarned"),
                    item.get("numOfDefensesRemaining"),
                    item.get("progressEarnedFromDefenses"),
                    observed_at,
                    _json_or_none(item),
                ),
            )


@managed_connection
def store_war_log(race_log, clan_tag, conn=None):
    clan_tag = _tag_key(clan_tag)
    _store_raw_payload(conn, "clan_war_log", clan_tag, race_log)
    stored = 0
    for entry in (race_log or {}).get("items", []):
        season_id = entry.get("seasonId")
        section_index = entry.get("sectionIndex")
        standings = entry.get("standings", [])
        our = None
        for standing in standings:
            clan = standing.get("clan", {})
            if _tag_key(clan.get("tag")) == clan_tag:
                our = standing
                break
        total_clans = len(standings)
        trophy_change = our.get("trophyChange") if our else None
        our_rank = our.get("rank") if our else None
        clan = (our or {}).get("clan", {})
        cur = conn.execute(
            "INSERT OR IGNORE INTO war_races (season_id, section_index, created_date, our_rank, trophy_change, our_fame, our_clan_score, total_clans, finish_time, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (season_id, section_index, entry.get("createdDate"), our_rank, trophy_change, clan.get("fame"), clan.get("clanScore"), total_clans, clan.get("finishTime"), _json_or_none(entry)),
        )
        if cur.rowcount == 0:
            race_row = conn.execute("SELECT war_race_id FROM war_races WHERE season_id = ? AND section_index = ?", (season_id, section_index)).fetchone()
            war_race_id = race_row["war_race_id"]
        else:
            war_race_id = cur.lastrowid
            stored += 1

        if our:
            for participant in clan.get("participants", []):
                ptag = _canon_tag(participant.get("tag"))
                member_id = _ensure_member(conn, ptag, participant.get("name"), status=None) if ptag else None
                conn.execute(
                    "INSERT OR REPLACE INTO war_participation (war_race_id, member_id, player_tag, player_name, fame, repair_points, boat_attacks, decks_used, decks_used_today, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (war_race_id, member_id, ptag, participant.get("name"), participant.get("fame", 0), participant.get("repairPoints", 0), participant.get("boatAttacks", 0), participant.get("decksUsed", 0), participant.get("decksUsedToday", 0), _json_or_none(participant)),
                )
    conn.commit()
    return stored


@managed_connection
def upsert_war_current_state(war_data, conn=None):
    observed_at = _utcnow()
    clan = (war_data or {}).get("clan", {})
    conn.execute(
        "INSERT INTO war_current_state (observed_at, war_state, clan_tag, clan_name, fame, repair_points, period_points, clan_score, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (observed_at, war_data.get("state"), _canon_tag(clan.get("tag")), clan.get("name"), clan.get("fame"), clan.get("repairPoints"), clan.get("periodPoints"), clan.get("clanScore"), _json_or_none(war_data)),
    )
    latest_logged_race = _get_latest_logged_race(conn)
    season_id = _infer_current_season_id_from_live_state(war_data, latest_logged_race)
    section_index = war_data.get("sectionIndex")
    period_index = war_data.get("periodIndex")
    phase = _resolve_phase(war_data.get("periodType"), period_index)
    phase_day_number = _phase_day_number(phase, period_index)
    battle_date = _war_day_key(season_id, section_index, period_index, observed_at)
    for participant in clan.get("participants", []):
        member_id = _ensure_member(conn, participant.get("tag"), participant.get("name"), status=None)
        conn.execute(
            "INSERT INTO war_day_status (member_id, battle_date, observed_at, fame, repair_points, boat_attacks, decks_used_total, decks_used_today, season_id, section_index, period_index, phase, phase_day_number, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(member_id, battle_date) DO UPDATE SET observed_at = excluded.observed_at, fame = excluded.fame, repair_points = excluded.repair_points, boat_attacks = excluded.boat_attacks, decks_used_total = excluded.decks_used_total, decks_used_today = excluded.decks_used_today, season_id = excluded.season_id, section_index = excluded.section_index, period_index = excluded.period_index, phase = excluded.phase, phase_day_number = excluded.phase_day_number, raw_json = excluded.raw_json",
            (member_id, battle_date, observed_at, participant.get("fame", 0), participant.get("repairPoints", 0), participant.get("boatAttacks", 0), participant.get("decksUsed", 0), participant.get("decksUsedToday", 0), season_id, section_index, period_index, phase, phase_day_number, _json_or_none(participant)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO war_participant_snapshots (observed_at, war_day_key, season_id, section_index, period_index, phase, phase_day_number, clan_tag, clan_name, member_id, player_tag, player_name, fame, repair_points, boat_attacks, decks_used_total, decks_used_today, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observed_at,
                battle_date,
                season_id,
                section_index,
                period_index,
                phase,
                phase_day_number,
                _canon_tag(clan.get("tag")),
                clan.get("name"),
                member_id,
                _canon_tag(participant.get("tag")),
                participant.get("name"),
                participant.get("fame", 0),
                participant.get("repairPoints", 0),
                participant.get("boatAttacks", 0),
                participant.get("decksUsed", 0),
                participant.get("decksUsedToday", 0),
                _json_or_none(participant),
            ),
        )
    _upsert_period_logs(conn, observed_at, war_data, season_id)
    conn.commit()


# -- Player profiles and battle facts --------------------------------------
