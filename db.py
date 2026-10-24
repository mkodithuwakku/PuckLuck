"""PuckLuck local data warehouse (SQLite).

Implements the relational schema defined in SRS §3.2 exactly. DDL is
idempotent (``CREATE TABLE IF NOT EXISTS``) and every connection opened
through :func:`connect` enforces ``PRAGMA foreign_keys = ON``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Union

DEFAULT_DB_PATH = Path("data/puckluck.sqlite3")

PathLike = Union[str, Path]

SCHEMA_SQL = """
-- 1. Identity Crosswalk
CREATE TABLE IF NOT EXISTS dim_players (
    player_id INTEGER PRIMARY KEY,          -- Official NHL.com Player ID
    yahoo_player_id TEXT UNIQUE,            -- Yahoo Fantasy Player Key
    canonical_name TEXT NOT NULL,           -- Lowercase normalized (e.g. "connor mcdavid")
    display_name TEXT NOT NULL,             -- Formatted ("Connor McDavid")
    primary_position TEXT NOT NULL,         -- C, LW, RW, D, G
    shoots_catches TEXT,                    -- L, R
    birth_date TEXT                         -- YYYY-MM-DD
);
CREATE INDEX IF NOT EXISTS idx_players_canonical ON dim_players(canonical_name);

-- 2. Team Dimension
CREATE TABLE IF NOT EXISTS dim_teams (
    team_id INTEGER PRIMARY KEY,            -- NHL API Team ID
    abbreviation TEXT NOT NULL UNIQUE,      -- EDM, FLA, NYR, etc.
    team_name TEXT NOT NULL,
    venue TEXT NOT NULL,
    altitude_ft INTEGER DEFAULT 0           -- Altitude for fatigue modeling (e.g. DEN = 5280)
);

-- 3. Fact: Skater Box Scores with Advanced Process Metrics
CREATE TABLE IF NOT EXISTS fact_skater_games (
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL REFERENCES dim_players(player_id),
    season INTEGER NOT NULL,
    game_date TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    opponent_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    is_home INTEGER NOT NULL,
    rest_days INTEGER NOT NULL,             -- 0 = Back-to-Back, 1 = 1 day, >=2
    toi_seconds INTEGER NOT NULL,
    pp_toi_seconds INTEGER NOT NULL,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    shots_on_goal INTEGER NOT NULL DEFAULT 0,
    blocks INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    faceoffs_won INTEGER NOT NULL DEFAULT 0,
    pim INTEGER NOT NULL DEFAULT 0,
    -- Advanced MoneyPuck Metrics:
    ixg REAL NOT NULL DEFAULT 0.0,          -- Individual Expected Goals
    icorsi INTEGER NOT NULL DEFAULT 0,      -- Individual Shot Attempts
    ihdcf INTEGER NOT NULL DEFAULT 0,       -- Individual High Danger Chances
    on_ice_xg_share REAL DEFAULT 0.0,       -- On-ice xGF% at 5v5
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_skaters_date ON fact_skater_games(game_date);
CREATE INDEX IF NOT EXISTS idx_skaters_lookup ON fact_skater_games(player_id, game_date);

-- 4. Fact: Goalie Performances & Defense Suppression
CREATE TABLE IF NOT EXISTS fact_goalie_games (
    game_id INTEGER NOT NULL,
    goalie_id INTEGER NOT NULL REFERENCES dim_players(player_id),
    season INTEGER NOT NULL,
    game_date TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    opponent_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    is_home INTEGER NOT NULL,
    decision TEXT,                          -- W, L, OTL
    toi_seconds INTEGER NOT NULL,
    shots_against INTEGER NOT NULL,
    goals_against INTEGER NOT NULL,
    saves INTEGER NOT NULL,
    gsax REAL NOT NULL,                     -- Goals Saved Above Expected
    hd_save_pct REAL,                       -- High Danger Save Percentage
    PRIMARY KEY (game_id, goalie_id)
);

-- 5. Stored Betting & Market Feeds
CREATE TABLE IF NOT EXISTS fact_market_lines (
    game_id INTEGER NOT NULL,
    sportsbook TEXT NOT NULL,
    market_type TEXT NOT NULL,              -- TOTAL_GOALS, SPREAD, PUCK_LINE, PLAYER_POINTS
    target_id TEXT,                         -- NULL for game total, player_id for props
    line REAL NOT NULL,                     -- e.g. 6.5 goals, 1.5 points
    over_odds INTEGER NOT NULL,             -- American odds (-110)
    under_odds INTEGER NOT NULL,            -- American odds (-110)
    captured_at TEXT NOT NULL,
    PRIMARY KEY (game_id, sportsbook, market_type, target_id)
);
"""

EXPECTED_TABLES = (
    "dim_players",
    "dim_teams",
    "fact_skater_games",
    "fact_goalie_games",
    "fact_market_lines",
)


def connect(db_path: PathLike = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with Row factory and FK enforcement enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: PathLike = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create the warehouse (and parent dirs) if needed; return a connection."""
    path = Path(db_path)
    if str(db_path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def upsert_player(
    conn: sqlite3.Connection,
    player_id: int,
    display_name: str,
    primary_position: str,
    *,
    canonical_name: Optional[str] = None,
    yahoo_player_id: Optional[str] = None,
    shoots_catches: Optional[str] = None,
    birth_date: Optional[str] = None,
) -> None:
    """Insert or update a player keyed by NHL.com ``player_id``."""
    canonical = canonical_name or display_name.strip().lower()
    conn.execute(
        """
        INSERT INTO dim_players (
            player_id, yahoo_player_id, canonical_name, display_name,
            primary_position, shoots_catches, birth_date
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
            canonical_name = excluded.canonical_name,
            display_name = excluded.display_name,
            primary_position = excluded.primary_position,
            yahoo_player_id = COALESCE(
                excluded.yahoo_player_id, dim_players.yahoo_player_id
            ),
            shoots_catches = COALESCE(
                excluded.shoots_catches, dim_players.shoots_catches
            ),
            birth_date = COALESCE(excluded.birth_date, dim_players.birth_date)
        """,
        (
            player_id,
            yahoo_player_id,
            canonical,
            display_name,
            primary_position,
            shoots_catches,
            birth_date,
        ),
    )


def upsert_team(
    conn: sqlite3.Connection,
    team_id: int,
    abbreviation: str,
    team_name: str,
    venue: str,
    *,
    altitude_ft: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO dim_teams (team_id, abbreviation, team_name, venue, altitude_ft)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(team_id) DO UPDATE SET
            abbreviation = excluded.abbreviation,
            team_name = excluded.team_name,
            venue = excluded.venue,
            altitude_ft = excluded.altitude_ft
        """,
        (team_id, abbreviation, team_name, venue, altitude_ft),
    )


def insert_skater_game(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one skater box score; duplicate (game_id, player_id) is ignored."""
    conn.execute(
        """
        INSERT OR IGNORE INTO fact_skater_games (
            game_id, player_id, season, game_date, team_id, opponent_id,
            is_home, rest_days, toi_seconds, pp_toi_seconds, goals, assists,
            shots_on_goal, blocks, hits, faceoffs_won, pim,
            ixg, icorsi, ihdcf, on_ice_xg_share
        ) VALUES (
            :game_id, :player_id, :season, :game_date, :team_id, :opponent_id,
            :is_home, :rest_days, :toi_seconds, :pp_toi_seconds,
            :goals, :assists, :shots_on_goal, :blocks, :hits,
            :faceoffs_won, :pim, :ixg, :icorsi, :ihdcf, :on_ice_xg_share
        )
        """,
        row,
    )


def insert_goalie_game(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one goalie box score; duplicate (game_id, goalie_id) is ignored."""
    conn.execute(
        """
        INSERT OR IGNORE INTO fact_goalie_games (
            game_id, goalie_id, season, game_date, team_id, opponent_id,
            is_home, decision, toi_seconds, shots_against, goals_against,
            saves, gsax, hd_save_pct
        ) VALUES (
            :game_id, :goalie_id, :season, :game_date, :team_id, :opponent_id,
            :is_home, :decision, :toi_seconds, :shots_against, :goals_against,
            :saves, :gsax, :hd_save_pct
        )
        """,
        row,
    )


def insert_market_line(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or replace a captured market line.

    Note: per SQLite semantics, rows with NULL ``target_id`` (game totals)
    never conflict on the composite PK, so callers should dedupe on
    (game_id, sportsbook, market_type) before inserting totals.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO fact_market_lines (
            game_id, sportsbook, market_type, target_id, line,
            over_odds, under_odds, captured_at
        ) VALUES (
            :game_id, :sportsbook, :market_type, :target_id, :line,
            :over_odds, :under_odds, :captured_at
        )
        """,
        row,
    )
