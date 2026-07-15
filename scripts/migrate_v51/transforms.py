"""v5.1 migration transforms T1–T14 (docs/reference/v5.1/migration.md Phase 3).

Reads the read-only archive, writes the new DB. Every transform is
idempotent: it clears its target table(s) and reloads. Run order matters
only where noted (T3 before T9 — clans FK; T1 before everything tag-keyed).

Usage:
    ./venv/bin/python scripts/migrate_v51/transforms.py \
        --archive elixir-v5-archive-2026H2.db --db elixir-v51.db

Semantics pinned here (and mirrored by parity_checks.py):
- The max season_id in war_races is treated as IN PROGRESS: its war_seasons
  row carries NULL final_rank/ended_at (the new engine closes it at the real
  season_closed event).
- free_pass seeds from rank-1 war_champ rows only (Q2 erratum).
- Old war_participation joins through war_races for (season_id, section_index);
  observed_at proxies from the race row (finish_time, else created_date).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

CALENDAR_TYPES = (
    "member_birthday",
    "clan_birthday",
    "join_anniversary",
    "weekly_donation_leader",
)
DEPRECATED_AWARD_TYPES = ("perfect_week", "victory_lap", "donation_champ_weekly")
HOME_CLAN = "#J2RGCRVG"

VERBATIM_TABLES = [
    # T10 (tournament_battles/participants handled separately — column drops)
    "tournaments",
    # T11 (decision_cases/revisits handled separately — renames)
    "leader_action_recommendations",
    # T12 conversation set
    "conversation_threads", "messages", "memory_facts", "memory_episodes",
    # T13 ops singletons
    "llm_calls", "prompt_failures", "prompt_feedback", "system_signals",
    "api_sentinel_observations", "arena_relay_screenshot_observations",
    "discord_channels", "channel_state", "game_mode_contexts", "card_catalog",
    "elixir_improvement_suggestions", "runtime_job_status",
    # T4 half
    "discord_users",
]


def _clear(conn, *tables):
    for t in tables:
        conn.execute(f"DELETE FROM {t}")


def t1_players(conn):
    _clear(conn, "player_aliases", "players")
    conn.execute(
        """INSERT INTO players (player_tag, current_name, first_seen_at, last_seen_at)
           SELECT player_tag, current_name, first_seen_at, last_seen_at FROM arch.members"""
    )
    conn.execute(
        """INSERT INTO player_aliases (player_tag, alias, source, observed_at)
           SELECT m.player_tag, a.alias, a.source, a.observed_at
           FROM arch.member_aliases a JOIN arch.members m ON m.member_id = a.member_id"""
    )


def t2_player_metadata(conn):
    _clear(conn, "player_metadata")
    conn.execute(
        """INSERT INTO player_metadata (
               player_tag, joined_at, birth_month, birth_day, profile_url, note,
               generated_bio, generated_highlight, generated_profile_updated_at,
               cr_account_age_days, cr_account_age_years, cr_account_age_updated_at,
               cr_games_per_day, cr_games_per_day_window_days, cr_games_per_day_updated_at,
               cr_collection_level, cr_collection_level_badge_tier,
               cr_collection_level_badge_max_tier, cr_collection_level_updated_at,
               cr_clan_war_wins, cr_battle_wins, cr_clan_donations,
               cr_banner_count, cr_emote_count, cr_profile_badges_updated_at)
           SELECT m.player_tag, md.joined_at, md.birth_month, md.birth_day,
               md.profile_url, md.note,
               md.generated_bio, md.generated_highlight, md.generated_profile_updated_at,
               md.cr_account_age_days, md.cr_account_age_years, md.cr_account_age_updated_at,
               md.cr_games_per_day, md.cr_games_per_day_window_days, md.cr_games_per_day_updated_at,
               md.cr_collection_level, md.cr_collection_level_badge_tier,
               md.cr_collection_level_badge_max_tier, md.cr_collection_level_updated_at,
               md.cr_clan_war_wins, md.cr_battle_wins, md.cr_clan_donations,
               md.cr_banner_count, md.cr_emote_count, md.cr_profile_badges_updated_at
           FROM arch.member_metadata md JOIN arch.members m ON m.member_id = md.member_id"""
    )  # poap_address deliberately dropped (Q4)


def t3_clans(conn):
    """Before T9 (war_week_clans FK). Sources: clan_daily_metrics (ours) +
    war_period_clan_status (full opponent history — the raw log is only 14d)."""
    _clear(conn, "clans")
    conn.execute(
        """INSERT INTO clans (clan_tag, name, first_seen_at, last_seen_at, is_home)
           SELECT clan_tag,
                  MAX(clan_name),
                  MIN(observed_at), MAX(observed_at),
                  CASE WHEN clan_tag = ? THEN 1 ELSE 0 END
           FROM (
               SELECT clan_tag, clan_name, observed_at FROM arch.clan_daily_metrics
               UNION ALL
               SELECT clan_tag, clan_name, observed_at FROM arch.war_period_clan_status
           )
           GROUP BY clan_tag""",
        (HOME_CLAN,),
    )


def t4_discord(conn):
    _clear(conn, "discord_links", "discord_users")
    conn.execute("INSERT INTO discord_users SELECT * FROM arch.discord_users")
    conn.execute(
        """INSERT INTO discord_links (discord_user_id, player_tag, linked_at,
                                      source, confidence, is_primary)
           SELECT dl.discord_user_id, m.player_tag, dl.linked_at,
                  dl.source, dl.confidence, dl.is_primary
           FROM arch.discord_links dl JOIN arch.members m ON m.member_id = dl.member_id"""
    )  # discord_username / discord_display_name dropped (duplicate discord_users)


def t5_memberships(conn):
    _clear(conn, "clan_memberships")
    conn.execute(
        """INSERT INTO clan_memberships (player_tag, clan_tag, joined_at, left_at,
                                         join_source, leave_source)
           SELECT m.player_tag, ?, cm.joined_at, cm.left_at,
                  cm.join_source, cm.leave_source
           FROM arch.clan_memberships cm JOIN arch.members m ON m.member_id = cm.member_id""",
        (HOME_CLAN,),
    )


def t6_awards(conn):
    deprecated = conn.execute(
        f"""SELECT COUNT(*) FROM arch.awards
            WHERE award_type IN ({','.join('?' * len(DEPRECATED_AWARD_TYPES))})""",
        DEPRECATED_AWARD_TYPES,
    ).fetchone()[0]
    if deprecated:
        raise SystemExit(f"unexpected deprecated award rows in archive: {deprecated}")
    _clear(conn, "awards")
    conn.execute(
        """INSERT INTO awards (award_type, season_id, section_index, player_tag,
                               rank, metric_value, metric_unit, metadata_json, awarded_at)
           SELECT award_type, season_id, section_index, player_tag,
                  rank, metric_value, metric_unit, metadata_json, awarded_at
           FROM arch.awards"""
    )
    # Q2/C5 rotation ledger: one free_pass per season, from rank-1 war_champ only.
    conn.execute(
        """INSERT INTO awards (award_type, season_id, section_index, player_tag,
                               rank, metric_value, metric_unit, metadata_json, awarded_at)
           SELECT 'free_pass', season_id, section_index, player_tag,
                  1, metric_value, metric_unit,
                  '{"seeded_from":"war_champ","note":"T6: historically champ = pass recipient"}',
                  awarded_at
           FROM arch.awards WHERE award_type = 'war_champ' AND rank = 1"""
    )


def t7_player_rollups(conn):
    _clear(conn, "player_daily_metrics", "player_daily_battle_rollups")
    conn.execute(
        """INSERT INTO player_daily_metrics (player_tag, metric_date, exp_level,
               trophies, best_trophies, clan_rank, donations_week,
               donations_received_week, last_seen_api)
           SELECT m.player_tag, d.metric_date, d.exp_level, d.trophies,
               d.best_trophies, d.clan_rank, d.donations_week,
               d.donations_received_week, d.last_seen_api
           FROM arch.member_daily_metrics d JOIN arch.members m ON m.member_id = d.member_id"""
    )
    conn.execute(
        """INSERT INTO player_daily_battle_rollups (player_tag, battle_date, mode_group,
               game_mode_id, game_mode_name, battles, wins, losses, draws,
               crowns_for, crowns_against, trophy_change_total,
               first_battle_at, last_battle_at, captured_battles,
               expected_battle_delta, completeness_ratio, is_complete, last_aggregated_at)
           SELECT m.player_tag, b.battle_date, b.mode_group,
               b.game_mode_id, b.game_mode_name, b.battles, b.wins, b.losses, b.draws,
               b.crowns_for, b.crowns_against, b.trophy_change_total,
               b.first_battle_at, b.last_battle_at, b.captured_battles,
               b.expected_battle_delta, b.completeness_ratio, b.is_complete, b.last_aggregated_at
           FROM arch.member_daily_battle_rollups b JOIN arch.members m ON m.member_id = b.member_id"""
    )


def t8_clan_rollups(conn):
    _clear(conn, "clan_daily_metrics", "clan_daily_battle_rollups")
    conn.execute(
        """INSERT INTO clan_daily_metrics (metric_date, clan_tag, clan_name,
               member_count, open_slots, clan_score, clan_war_trophies,
               required_trophies, donations_per_week_requirement, weekly_donations_total,
               total_member_trophies, avg_member_trophies, top_member_trophies,
               joins_today, leaves_today, net_member_change, observed_at)
           SELECT metric_date, clan_tag, clan_name,
               member_count, open_slots, clan_score, clan_war_trophies,
               required_trophies, donations_per_week_requirement, weekly_donations_total,
               total_member_trophies, avg_member_trophies, top_member_trophies,
               joins_today, leaves_today, net_member_change, observed_at
           FROM arch.clan_daily_metrics"""
    )  # raw_json dropped (L1 owns raw)
    conn.execute("INSERT INTO clan_daily_battle_rollups SELECT * FROM arch.clan_daily_battle_rollups")


def t9_war(conn):
    """After T3 (clans FK) and T6 (champ/free-pass tags). Max season = in progress."""
    _clear(conn, "war_attendance_days", "war_participation", "war_week_clans",
           "war_weeks", "war_seasons")
    conn.execute(
        """INSERT INTO war_seasons (season_id, started_at, ended_at, final_rank,
                                    weeks, war_champ_tag, free_pass_tag)
           SELECT r.season_id,
                  MIN(COALESCE(r.created_date, r.finish_time, '')),
                  CASE WHEN r.season_id < (SELECT MAX(season_id) FROM arch.war_races)
                       THEN MAX(COALESCE(r.finish_time, r.created_date)) END,
                  CASE WHEN r.season_id < (SELECT MAX(season_id) FROM arch.war_races)
                       THEN (SELECT our_rank FROM arch.war_races r2
                             WHERE r2.season_id = r.season_id
                             ORDER BY section_index DESC LIMIT 1) END,
                  COUNT(*),
                  (SELECT player_tag FROM arch.awards a
                   WHERE a.award_type='war_champ' AND a.rank=1 AND a.season_id=r.season_id),
                  (SELECT player_tag FROM arch.awards a
                   WHERE a.award_type='war_champ' AND a.rank=1 AND a.season_id=r.season_id)
           FROM arch.war_races r GROUP BY r.season_id"""
    )  # free_pass_tag = champ historically (T6 note)
    conn.execute(
        """INSERT INTO war_weeks (season_id, section_index, period_type, created_date,
                                  finish_time, our_rank, our_fame, trophy_change, our_clan_score)
           SELECT season_id, section_index, NULL, created_date,
                  finish_time, our_rank, our_fame, trophy_change, our_clan_score
           FROM arch.war_races"""
    )  # period_type unknowable historically; NULL by design
    conn.execute(
        """INSERT INTO war_week_clans (season_id, section_index, clan_tag, fame,
                                       rank, clan_score, completed_at, observed_at)
           SELECT season_id, section_index, clan_tag,
                  progress_end_of_day, end_of_day_rank, NULL, NULL, MAX(observed_at)
           FROM arch.war_period_clan_status
           WHERE season_id IS NOT NULL AND section_index IS NOT NULL
           GROUP BY season_id, section_index, clan_tag
           HAVING period_index = MAX(period_index)"""
    )
    conn.execute(
        """INSERT INTO war_participation (season_id, section_index, player_tag, fame,
               repair_points, boat_attacks, decks_used, decks_used_today, observed_at)
           SELECT r.season_id, r.section_index, p.player_tag, p.fame,
               p.repair_points, p.boat_attacks, p.decks_used, p.decks_used_today,
               COALESCE(r.finish_time, r.created_date, 'migrated')
           FROM arch.war_participation p JOIN arch.war_races r ON r.war_race_id = p.war_race_id"""
    )
    # war_attendance_days starts empty by design (per-day history not reliably
    # reconstructable; evaluators tolerate a warm-up season).


def t10_tournaments(conn):
    _clear(conn, "tournament_battles", "tournament_participants")
    conn.execute(
        """INSERT INTO tournament_participants (participant_id, tournament_id,
               player_tag, player_name, clan_tag, first_seen_at, last_seen_at,
               final_score, final_rank)
           SELECT participant_id, tournament_id,
               player_tag, player_name, clan_tag, first_seen_at, last_seen_at,
               final_score, final_rank
           FROM arch.tournament_participants"""
    )  # member_id dropped (player_tag already present)
    conn.execute(
        """INSERT INTO tournament_battles (tournament_battle_id, tournament_id, battle_time,
               player1_tag, player1_name, player1_crowns, player1_deck_json,
               player2_tag, player2_name, player2_crowns, player2_deck_json,
               winner_tag, deck_selection, game_mode_id, arena_name, raw_json)
           SELECT tournament_battle_id, tournament_id, battle_time,
               player1_tag, player1_name, player1_crowns, player1_deck_json,
               player2_tag, player2_name, player2_crowns, player2_deck_json,
               winner_tag, deck_selection, game_mode_id, arena_name, raw_json
           FROM arch.tournament_battles"""
    )  # player*_member_id dropped


def t11_cases_revisits(conn):
    _clear(conn, "decision_cases", "revisits")
    conn.execute(
        """INSERT INTO decision_cases (case_id, case_key, case_type, status,
               subject_type, subject_key, target_player_tag, target_player_name,
               title, recommendation, rationale, priority,
               source_event_key, source_event_type,
               opened_at, due_at, resolved_at, resolution, state_json,
               created_at, updated_at)
           SELECT case_id, case_key, case_type, status,
               subject_type, subject_key, target_player_tag, target_player_name,
               title, recommendation, rationale, priority,
               COALESCE(source_event_key, source_signal_key), source_signal_type,
               opened_at, due_at, resolved_at, resolution, state_json,
               created_at, updated_at
           FROM arch.decision_cases"""
    )
    conn.execute(
        """INSERT INTO revisits (revisit_id, revisit_key, created_by_workflow,
                                 due_at, rationale, revisited_at, created_at)
           SELECT revisit_id, signal_key, created_by_workflow,
                  due_at, rationale, revisited_at, created_at
           FROM arch.revisits"""
    )


def t_verbatim(conn):
    for table in VERBATIM_TABLES:
        conn.execute(f"DELETE FROM {table}")
        conn.execute(f"INSERT INTO {table} SELECT * FROM arch.{table}")


def t14_ledger_seed(conn):
    """Calendar re-post guard: claim the trailing-14-day calendar keys."""
    conn.execute("DELETE FROM recognition_ledger WHERE stream = 'migration-seed:clan'")
    conn.execute(
        f"""INSERT OR IGNORE INTO recognition_ledger
                (recognition_key, stream, event_refs_json, score, claimed_at)
            SELECT dedup_key, 'migration-seed:clan',
                   json_array(dedup_key), 0, occurred_at
            FROM arch.detections
            WHERE detection_type IN ({','.join('?' * len(CALENDAR_TYPES))})
              AND occurred_at >= strftime('%Y%m%dT%H%M%S', 'now', '-14 days')""",
        CALENDAR_TYPES,
    )


TRANSFORMS = [
    ("T1 players + aliases", t1_players),
    ("T2 player_metadata", t2_player_metadata),
    ("T3 clans (before T9)", t3_clans),
    ("T4 discord users + links", t4_discord),
    ("T5 clan_memberships", t5_memberships),
    ("T6 awards + free_pass seed", t6_awards),
    ("T7 player rollups", t7_player_rollups),
    ("T8 clan rollups", t8_clan_rollups),
    ("T9 war (seasons/weeks/clans/participation)", t9_war),
    ("T10 tournament_battles", t10_tournaments),
    ("T11 decision_cases + revisits", t11_cases_revisits),
    ("T12/T13 verbatim set", t_verbatim),
    ("T14 recognition ledger calendar seed", t14_ledger_seed),
]


def run(archive_path: str, db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")  # loaded in bulk; checked at the end
    conn.execute(f"ATTACH DATABASE 'file:{archive_path}?immutable=1' AS arch")
    for name, fn in TRANSFORMS:
        fn(conn)
        print(f"[transform] {name}: done")
    conn.commit()
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        for v in violations[:20]:
            print(f"FK violation: {tuple(v)}")
        print(f"{len(violations)} foreign-key violations — transforms are NOT clean")
        return 1
    print("foreign_key_check clean")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default="elixir-v5-archive-2026H2.db")
    parser.add_argument("--db", default="elixir-v51.db")
    args = parser.parse_args()
    return run(args.archive, args.db)


if __name__ == "__main__":
    sys.exit(main())
