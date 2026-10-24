"""Tests for the SRS §3.2 SQLite warehouse (db.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = db.init_db(":memory:")
    yield c
    c.close()


def _seed_team_and_player(conn: sqlite3.Connection) -> None:
    db.upsert_team(conn, 25, "EDM", "Edmonton Oilers", "Rogers Place")
    db.upsert_team(conn, 13, "FLA", "Florida Panthers", "Amerant Bank Arena")
    db.upsert_player(conn, 8478402, "Connor McDavid", "C")


def test_all_tables_created(conn: sqlite3.Connection) -> None:
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in db.EXPECTED_TABLES:
        assert table in names


def test_foreign_keys_pragma_enabled(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "wh" / "puckluck.sqlite3"
    db.init_db(path).close()
    db.init_db(path).close()  # second run must not raise
    assert path.is_file()


def test_skater_fact_enforces_player_fk(conn: sqlite3.Connection) -> None:
    _seed_team_and_player(conn)
    row = {
        "game_id": 1,
        "player_id": 999,  # no such dim_players row
        "season": 2026,
        "game_date": "2026-10-22",
        "team_id": 25,
        "opponent_id": 13,
        "is_home": 1,
        "rest_days": 2,
        "toi_seconds": 1200,
        "pp_toi_seconds": 60,
        "goals": 0,
        "assists": 0,
        "shots_on_goal": 0,
        "blocks": 0,
        "hits": 0,
        "faceoffs_won": 0,
        "pim": 0,
        "ixg": 0.0,
        "icorsi": 0,
        "ihdcf": 0,
        "on_ice_xg_share": 0.0,
    }
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_skater_game(conn, row)


def test_skater_fact_enforces_team_fk(conn: sqlite3.Connection) -> None:
    db.upsert_player(conn, 8478402, "Connor McDavid", "C")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fact_skater_games (game_id, player_id, season,"
            " game_date, team_id, opponent_id, is_home, rest_days,"
            " toi_seconds, pp_toi_seconds) VALUES (1, 8478402, 2026,"
            " '2026-10-22', 999, 998, 1, 2, 0, 0)"
        )


def test_player_upsert_round_trip_and_preserves_yahoo(
    conn: sqlite3.Connection,
) -> None:
    db.upsert_player(
        conn,
        8478402,
        "Connor McDavid",
        "C",
        canonical_name="connor mcdavid",
        yahoo_player_id="449.p.6743",
        shoots_catches="L",
        birth_date="1997-01-13",
    )
    db.upsert_player(conn, 8478402, "Connor McDavid", "C")  # partial re-upsert
    row = conn.execute("SELECT * FROM dim_players WHERE player_id = 8478402").fetchone()
    assert row["canonical_name"] == "connor mcdavid"
    assert row["yahoo_player_id"] == "449.p.6743"  # not clobbered by NULL
    assert row["shoots_catches"] == "L"


def test_yahoo_player_id_unique(conn: sqlite3.Connection) -> None:
    db.upsert_player(conn, 1, "Player One", "C", yahoo_player_id="449.p.1")
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_player(conn, 2, "Player Two", "D", yahoo_player_id="449.p.1")


def test_skater_game_composite_pk_dedupes(conn: sqlite3.Connection) -> None:
    _seed_team_and_player(conn)
    row = {
        "game_id": 1,
        "player_id": 8478402,
        "season": 2026,
        "game_date": "2026-10-22",
        "team_id": 25,
        "opponent_id": 13,
        "is_home": 1,
        "rest_days": 2,
        "toi_seconds": 1200,
        "pp_toi_seconds": 60,
        "goals": 1,
        "assists": 0,
        "shots_on_goal": 4,
        "blocks": 0,
        "hits": 0,
        "faceoffs_won": 9,
        "pim": 0,
        "ixg": 0.45,
        "icorsi": 6,
        "ihdcf": 2,
        "on_ice_xg_share": 0.61,
    }
    db.insert_skater_game(conn, row)
    db.insert_skater_game(conn, dict(row, goals=5))  # dup PK -> ignored
    fetched = conn.execute(
        "SELECT * FROM fact_skater_games WHERE game_id = 1"
    ).fetchall()
    assert len(fetched) == 1
    assert fetched[0]["goals"] == 1


def test_goalie_game_insert(conn: sqlite3.Connection) -> None:
    db.upsert_team(conn, 25, "EDM", "Edmonton Oilers", "Rogers Place")
    db.upsert_team(conn, 13, "FLA", "Florida Panthers", "Amerant Bank Arena")
    db.upsert_player(conn, 8479979, "Stuart Skinner", "G")
    db.insert_goalie_game(
        conn,
        {
            "game_id": 1,
            "goalie_id": 8479979,
            "season": 2026,
            "game_date": "2026-10-22",
            "team_id": 25,
            "opponent_id": 13,
            "is_home": 0,
            "decision": "W",
            "toi_seconds": 3600,
            "shots_against": 31,
            "goals_against": 2,
            "saves": 29,
            "gsax": 1.13,
            "hd_save_pct": 0.857,
        },
    )
    row = conn.execute("SELECT * FROM fact_goalie_games").fetchone()
    assert row["gsax"] == pytest.approx(1.13)
    assert row["decision"] == "W"


def test_market_line_insert_replace(conn: sqlite3.Connection) -> None:
    row = {
        "game_id": 100,
        "sportsbook": "consensus",
        "market_type": "TOTAL_GOALS",
        "target_id": "8478402",
        "line": 1.5,
        "over_odds": -110,
        "under_odds": -110,
        "captured_at": "2026-10-23T06:00:00",
    }
    db.insert_market_line(conn, row)
    db.insert_market_line(conn, dict(row, over_odds=+105))
    fetched = conn.execute("SELECT * FROM fact_market_lines").fetchall()
    assert len(fetched) == 1
    assert fetched[0]["over_odds"] == 105


def test_expected_indexes_exist(conn: sqlite3.Connection) -> None:
    names = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_players_canonical" in names
    assert "idx_skaters_date" in names
    assert "idx_skaters_lookup" in names
