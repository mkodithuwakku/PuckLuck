"""Tests for Official NHL API ingestion (ingestion/nhl.py).

All fetches flow through stubbed JsonFetcher callables; conftest.py
blocks real sockets.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

import db
from ingestion import nhl


def _skater_row(
    game_id: int, player_id: int, team_id: int, opp_id: int, game_date: str
) -> Dict[str, Any]:
    return {
        "game_id": game_id,
        "player_id": player_id,
        "season": 2026,
        "game_date": game_date,
        "team_id": team_id,
        "opponent_id": opp_id,
        "is_home": 1,
        "rest_days": 2,
        "toi_seconds": 1200,
        "pp_toi_seconds": 0,
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


SCHEDULE_PAYLOAD: Dict[str, Any] = {
    "gameWeek": [
        {
            "date": "2026-10-22",
            "games": [
                {
                    "id": 2026020000,
                    "season": 20262027,
                    "gameDate": "2026-10-22",
                    "startTimeUTC": "2026-10-23T00:00:00Z",
                    "venue": {"default": "T-Mobile Arena"},
                    "homeTeam": {
                        "id": 54,
                        "abbrev": "VGK",
                        "placeName": {"default": "Vegas"},
                        "commonName": {"default": "Golden Knights"},
                    },
                    "awayTeam": {
                        "id": 21,
                        "abbrev": "COL",
                        "placeName": {"default": "Colorado"},
                        "commonName": {"default": "Avalanche"},
                    },
                }
            ],
        },
        {
            "date": "2026-10-23",
            "games": [
                {
                    "id": 2026020001,
                    "season": 20262027,
                    "gameDate": "2026-10-23",
                    "startTimeUTC": "2026-10-24T01:00:00Z",
                    "venue": {"default": "Rogers Place"},
                    "homeTeam": {
                        "id": 25,
                        "abbrev": "EDM",
                        "placeName": {"default": "Edmonton"},
                        "commonName": {"default": "Oilers"},
                    },
                    "awayTeam": {
                        "id": 21,
                        "abbrev": "COL",
                        "placeName": {"default": "Colorado"},
                        "commonName": {"default": "Avalanche"},
                    },
                },
                {
                    "id": 2026020002,
                    "season": 20262027,
                    "gameDate": "2026-10-23",
                    "startTimeUTC": "2026-10-23T23:00:00Z",
                    "venue": {"default": "Scotiabank Arena"},
                    "homeTeam": {
                        "id": 10,
                        "abbrev": "TOR",
                        "placeName": {"default": "Toronto"},
                        "commonName": {"default": "Maple Leafs"},
                    },
                    "awayTeam": {
                        "id": 12,
                        "abbrev": "CAR",
                        "placeName": {"default": "Carolina"},
                        "commonName": {"default": "Hurricanes"},
                    },
                },
            ],
        },
    ]
}

ROSTER_PAYLOAD: Dict[str, Any] = {
    "forwards": [
        {
            "id": 8478402,
            "firstName": {"default": "Connor"},
            "lastName": {"default": "McDavid"},
            "positionCode": "C",
            "shootsCatches": "L",
            "birthDate": "1997-01-13",
        },
        {
            "id": 8476453,
            "firstName": {"default": "Leon"},
            "lastName": {"default": "Draisaitl"},
            "positionCode": "L",
            "shootsCatches": "L",
            "birthDate": "1995-10-27",
        },
    ],
    "defensemen": [
        {
            "id": 8480803,
            "firstName": {"default": "Evan"},
            "lastName": {"default": "Bouchard"},
            "positionCode": "D",
            "shootsCatches": "R",
            "birthDate": "1999-10-20",
        }
    ],
    "goalies": [
        {
            "id": 8479979,
            "firstName": {"default": "Stuart"},
            "lastName": {"default": "Skinner"},
            "positionCode": "G",
            "shootsCatches": "L",
            "birthDate": "1998-11-01",
        }
    ],
}

BOXSCORE_PAYLOAD: Dict[str, Any] = {
    "id": 2026020001,
    "season": 20262027,
    "gameDate": "2026-10-23",
    "venue": {"default": "Rogers Place"},
    "homeTeam": {
        "id": 25,
        "abbrev": "EDM",
        "placeName": {"default": "Edmonton"},
        "commonName": {"default": "Oilers"},
    },
    "awayTeam": {
        "id": 21,
        "abbrev": "COL",
        "placeName": {"default": "Colorado"},
        "commonName": {"default": "Avalanche"},
    },
    "playerByGameStats": {
        "awayTeam": {
            "forwards": [
                {
                    "playerId": 8480000,
                    "name": {"default": "N. MacKinnon"},
                    "position": "C",
                    "goals": 1,
                    "assists": 1,
                    "sog": 5,
                    "blockedShots": 0,
                    "hits": 2,
                    "faceoffsWon": 11,
                    "pim": 0,
                    "toi": "21:34",
                    "powerPlayToi": "2:15",
                }
            ],
            "defense": [],
            "goalies": [
                {
                    "playerId": 8481000,
                    "name": {"default": "S. Blackwood"},
                    "position": "G",
                    "decision": "L",
                    "shotsAgainst": 33,
                    "goalsAgainst": 4,
                    "toi": "59:47",
                }
            ],
        },
        "homeTeam": {
            "forwards": [
                {
                    "playerId": 8478402,
                    "name": {"default": "C. McDavid"},
                    "position": "C",
                    "goals": 2,
                    "assists": 1,
                    "sog": 6,
                    "blockedShots": 0,
                    "hits": 1,
                    "faceoffsWon": 9,
                    "pim": 0,
                    "toi": "22:10",
                    "powerPlayToi": "3:04",
                }
            ],
            "defense": [
                {
                    "playerId": 8480803,
                    "name": {"default": "E. Bouchard"},
                    "position": "D",
                    "goals": 0,
                    "assists": 2,
                    "sog": 3,
                    "blockedShots": 1,
                    "hits": 0,
                    "pim": 2,
                    "toi": "24:00",
                    "powerPlayToi": "4:10",
                }
            ],
            "goalies": [
                {
                    "playerId": 8479979,
                    "name": {"default": "S. Skinner"},
                    "position": "G",
                    "decision": "W",
                    "shotsAgainst": 28,
                    "goalsAgainst": 2,
                    "toi": "60:00",
                },
                {
                    "playerId": 8489999,
                    "name": {"default": "C. Pickard"},
                    "position": "G",
                    "shotsAgainst": 0,
                    "goalsAgainst": 0,
                    "toi": "00:00",
                },
            ],
        },
    },
}


class StubFetcher:
    """Records URLs and returns canned payloads keyed by substring."""

    def __init__(self, routes: Dict[str, Dict[str, Any]]) -> None:
        self.routes = routes
        self.urls: List[str] = []

    def __call__(self, url: str) -> Dict[str, Any]:
        self.urls.append(url)
        for key, payload in self.routes.items():
            if key in url:
                return payload
        raise AssertionError(f"unstubbed url {url}")


def test_parse_toi_to_seconds() -> None:
    assert nhl.parse_toi_to_seconds("21:34") == 1294
    assert nhl.parse_toi_to_seconds("0:45") == 45
    assert nhl.parse_toi_to_seconds("00:00") == 0
    assert nhl.parse_toi_to_seconds(None) == 0
    assert nhl.parse_toi_to_seconds("") == 0
    assert nhl.parse_toi_to_seconds(95) == 95
    with pytest.raises(ValueError):
        nhl.parse_toi_to_seconds("twelve minutes")
    with pytest.raises(ValueError):
        nhl.parse_toi_to_seconds("7.5")


def test_normalize_nhl_position() -> None:
    assert nhl.normalize_nhl_position("L") == "LW"
    assert nhl.normalize_nhl_position("R") == "RW"
    assert nhl.normalize_nhl_position("C") == "C"
    assert nhl.normalize_nhl_position("D") == "D"
    assert nhl.normalize_nhl_position("G") == "G"
    assert nhl.normalize_nhl_position("LW") == "LW"
    assert nhl.normalize_nhl_position("RW") == "RW"
    assert nhl.normalize_nhl_position(None) == "C"


def test_fetch_schedule_filters_target_date() -> None:
    fetcher = StubFetcher({"/schedule/2026-10-23": SCHEDULE_PAYLOAD})
    games = nhl.fetch_schedule("2026-10-23", fetcher=fetcher)

    assert fetcher.urls == [f"{nhl.NHL_API_BASE}/schedule/2026-10-23"]
    assert len(games) == 2
    assert all(g.game_date == "2026-10-23" for g in games)

    edm = games[0]
    assert edm.game_id == 2026020001
    assert edm.home_abbr == "EDM"
    assert edm.home_name == "Edmonton Oilers"
    assert edm.away_abbr == "COL"
    assert edm.venue == "Rogers Place"
    assert edm.start_time_utc == "2026-10-24T01:00:00Z"


def test_ingest_team_roster_populates_dim_players() -> None:
    fetcher = StubFetcher({"/roster/EDM/current": ROSTER_PAYLOAD})
    conn = db.init_db(":memory:")
    count = nhl.ingest_team_roster(conn, "edm", fetcher=fetcher)
    assert count == 4
    assert fetcher.urls == [f"{nhl.NHL_API_BASE}/roster/EDM/current"]

    rows = {
        r["player_id"]: r for r in conn.execute("SELECT * FROM dim_players").fetchall()
    }
    assert rows[8478402]["canonical_name"] == "connor mcdavid"
    assert rows[8478402]["primary_position"] == "C"
    assert rows[8476453]["primary_position"] == "LW"  # "L" normalized
    assert rows[8476453]["birth_date"] == "1995-10-27"
    assert rows[8480803]["primary_position"] == "D"
    assert rows[8480803]["shoots_catches"] == "R"
    assert rows[8479979]["primary_position"] == "G"
    conn.close()


def test_compute_rest_days_temporal_integrity() -> None:
    conn = db.init_db(":memory:")
    db.upsert_team(conn, 25, "EDM", "Edmonton Oilers", "Rogers Place")
    db.upsert_team(conn, 21, "COL", "Colorado Avalanche", "Ball Arena")
    db.upsert_player(conn, 8478402, "Connor McDavid", "C")
    conn.commit()

    for gid, day in ((101, "2026-10-20"), (102, "2026-10-21"), (105, "2026-10-25")):
        db.insert_skater_game(conn, _skater_row(gid, 8478402, 25, 21, day))
    conn.commit()

    assert nhl.compute_rest_days(conn, 25, "2026-10-22") == 0  # B2B
    assert nhl.compute_rest_days(conn, 25, "2026-10-23") == 1
    # Future row (10-25) is invisible to 10-22/10-23 but visible to 10-26.
    assert nhl.compute_rest_days(conn, 25, "2026-10-26") == 0
    # No history -> default.
    assert nhl.compute_rest_days(conn, 21, "2026-10-22") == 2
    conn.close()


def test_ingest_boxscore_populates_facts_and_skips_zero_toi_goalie() -> None:
    fetcher = StubFetcher({"/gamecenter/2026020001/boxscore": BOXSCORE_PAYLOAD})
    conn = db.init_db(":memory:")
    metrics = {
        8478402: {"ixg": 0.85, "icorsi": 9, "ihdcf": 3, "on_ice_xg_share": 0.62},
        8479979: {"gsax": 1.42, "hd_save_pct": 0.875},
    }
    skaters, goalies = nhl.ingest_boxscore(
        conn, 2026020001, fetcher=fetcher, advanced_metrics=metrics
    )
    assert (skaters, goalies) == (3, 2)
    assert fetcher.urls == [f"{nhl.NHL_API_BASE}/gamecenter/2026020001/boxscore"]

    teams = {
        r["abbreviation"]: r for r in conn.execute("SELECT * FROM dim_teams").fetchall()
    }
    assert teams["COL"]["altitude_ft"] == 5280
    assert teams["EDM"]["altitude_ft"] == 2208
    assert teams["EDM"]["venue"] == "Rogers Place"
    assert teams["EDM"]["team_name"] == "Edmonton Oilers"

    mcdavid = conn.execute(
        "SELECT * FROM fact_skater_games WHERE player_id = 8478402"
    ).fetchone()
    assert mcdavid["game_id"] == 2026020001
    assert mcdavid["team_id"] == 25
    assert mcdavid["opponent_id"] == 21
    assert mcdavid["is_home"] == 1
    assert mcdavid["toi_seconds"] == 22 * 60 + 10
    assert mcdavid["pp_toi_seconds"] == 3 * 60 + 4
    assert mcdavid["goals"] == 2
    assert mcdavid["shots_on_goal"] == 6
    assert mcdavid["faceoffs_won"] == 9
    assert mcdavid["ixg"] == pytest.approx(0.85)
    assert mcdavid["icorsi"] == 9

    mackinnon = conn.execute(
        "SELECT * FROM fact_skater_games WHERE player_id = 8480000"
    ).fetchone()
    assert mackinnon["is_home"] == 0
    assert mackinnon["rest_days"] == 2  # no prior games -> default

    goalie_rows = conn.execute(
        "SELECT * FROM fact_goalie_games ORDER BY goalie_id"
    ).fetchall()
    assert [r["goalie_id"] for r in goalie_rows] == [8479979, 8481000]
    skinner = next(r for r in goalie_rows if r["goalie_id"] == 8479979)
    assert skinner["decision"] == "W"
    assert skinner["shots_against"] == 28
    assert skinner["goals_against"] == 2
    assert skinner["saves"] == 26  # derived: shots_against - goals_against
    assert skinner["gsax"] == pytest.approx(1.42)
    assert skinner["hd_save_pct"] == pytest.approx(0.875)

    # Backup goalie with 00:00 TOI is excluded entirely.
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM fact_goalie_games WHERE goalie_id = 8489999"
        ).fetchone()["n"]
        == 0
    )
    conn.close()
