"""Official NHL API ingestion (SRS §3.1 Source 2).

Pulls the daily schedule, current team rosters, and per-game boxscores
from the public ``api-web.nhle.com/v1`` REST API, normalizing payloads
into ``dim_players`` / ``dim_teams`` / ``fact_skater_games`` /
``fact_goalie_games``.

All network access flows through an injectable ``JsonFetcher`` so unit
tests run fully offline (SRS §8).
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import db
from ingestion.crosswalk import normalize_name

NHL_API_BASE = "https://api-web.nhle.com/v1"
USER_AGENT = "PuckLuck/0.1 (local analytics pipeline)"
HTTP_TIMEOUT_SECONDS = 30.0

JsonFetcher = Callable[[str], Dict[str, Any]]

# Home-venue altitude in feet (feeds SRS fatigue modeling). All other
# teams default to 0.
TEAM_ALTITUDE_FT: Dict[str, int] = {
    "COL": 5280,
    "UTA": 4226,
    "VGK": 2001,
    "CGY": 3438,
    "EDM": 2208,
    "WPG": 784,
}

_TOI_RE = re.compile(r"^\d+:\d{2}$")


@dataclass(frozen=True)
class ScheduledGame:
    game_id: int
    season: int
    game_date: str  # YYYY-MM-DD
    start_time_utc: str  # ISO-8601 UTC timestamp
    home_team_id: int
    home_abbr: str
    home_name: str
    away_team_id: int
    away_abbr: str
    away_name: str
    venue: str
    game_state: str = ""  # NHL gameState: FUT, PRE, LIVE, CRIT, OFF, FINAL, ...
    home_score: Optional[int] = None
    away_score: Optional[int] = None


# ---------------------------------------------------------------------------
# Default production transport (never used by tests — sockets are blocked).
# ---------------------------------------------------------------------------


def default_json_fetcher(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def parse_toi_to_seconds(toi: Optional[Union[str, int, float]]) -> int:
    """Convert NHL ``"MM:SS"`` ice-time strings (or numerics) to seconds."""
    if toi is None or toi == "":
        return 0
    if isinstance(toi, bool):
        raise ValueError(f"malformed TOI value: {toi!r}")
    if isinstance(toi, (int, float)):
        return int(toi)
    text = str(toi).strip()
    if not _TOI_RE.match(text):
        raise ValueError(f"malformed TOI value: {toi!r}")
    minutes, seconds = text.split(":")
    return int(minutes) * 60 + int(seconds)


def normalize_nhl_position(pos_code: Optional[str]) -> str:
    """Map NHL API position codes to warehouse positions."""
    mapping = {
        "L": "LW",
        "R": "RW",
        "C": "C",
        "D": "D",
        "G": "G",
        "LW": "LW",
        "RW": "RW",
    }
    if not pos_code:
        return "C"
    return mapping.get(str(pos_code).strip().upper(), "C")


def compute_rest_days(
    conn: sqlite3.Connection,
    team_id: int,
    game_date: str,
    *,
    default_rest: int = 2,
) -> int:
    """Days since the team's previous game (0 = back-to-back).

    Uses ``MAX(game_date) < game_date`` so tomorrow's stats can never
    leak into today's features (SRS §8 temporal integrity).
    """
    row = conn.execute(
        "SELECT MAX(game_date) AS prev FROM fact_skater_games "
        "WHERE team_id = ? AND game_date < ?",
        (team_id, game_date),
    ).fetchone()
    prev = row["prev"] if row else None
    if not prev:
        return default_rest
    gap = (date.fromisoformat(game_date) - date.fromisoformat(prev)).days
    return max(0, gap - 1)


def _local_name(field: Any) -> str:
    """Unwrap NHL ``{"default": "..."}`` localized strings."""
    if isinstance(field, dict):
        return str(field.get("default") or "")
    return str(field or "")


def _team_full_name(team: Dict[str, Any]) -> str:
    place = _local_name(team.get("placeName"))
    common = _local_name(team.get("commonName"))
    if place and common:
        return f"{place} {common}"
    if common:
        return common
    name = _local_name(team.get("name"))
    return name or place or str(team.get("abbrev") or "")


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _scheduled_game(raw: Dict[str, Any], game_date: str) -> ScheduledGame:
    home = raw.get("homeTeam") or {}
    away = raw.get("awayTeam") or {}
    return ScheduledGame(
        game_id=int(raw["id"]),
        season=int(raw.get("season") or str(raw["id"])[:4]),
        game_date=str(raw.get("gameDate") or game_date),
        start_time_utc=str(raw.get("startTimeUTC") or ""),
        home_team_id=int(home.get("id") or 0),
        home_abbr=str(home.get("abbrev") or ""),
        home_name=_team_full_name(home),
        away_team_id=int(away.get("id") or 0),
        away_abbr=str(away.get("abbrev") or ""),
        away_name=_team_full_name(away),
        venue=_local_name(raw.get("venue")),
        game_state=str(raw.get("gameState") or ""),
        home_score=_opt_int(home.get("score")),
        away_score=_opt_int(away.get("score")),
    )


def _opt_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def fetch_schedule(
    target_date: Union[str, date],
    *,
    fetcher: JsonFetcher = default_json_fetcher,
) -> List[ScheduledGame]:
    """GET ``/schedule/{yyyy-mm-dd}``; returns games for that date only."""
    iso = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
    payload = fetcher(f"{NHL_API_BASE}/schedule/{iso}")

    games: List[Dict[str, Any]] = []
    week = payload.get("gameWeek")
    if isinstance(week, list):
        for day in week:
            if not isinstance(day, dict):
                continue
            day_games = day.get("games") or []
            if day.get("date") == iso:
                games.extend(g for g in day_games if isinstance(g, dict))
    elif isinstance(payload.get("games"), list):
        games = [g for g in payload["games"] if isinstance(g, dict)]

    return [
        _scheduled_game(g, iso)
        for g in games
        if not g.get("gameDate") or g.get("gameDate") == iso
    ]


# ---------------------------------------------------------------------------
# Rosters
# ---------------------------------------------------------------------------


def _roster_full_name(player: Dict[str, Any]) -> str:
    first = _local_name(player.get("firstName"))
    last = _local_name(player.get("lastName"))
    return f"{first} {last}".strip()


def ingest_team_roster(
    conn: sqlite3.Connection,
    team_abbr: str,
    *,
    fetcher: JsonFetcher = default_json_fetcher,
    commit: bool = True,
) -> int:
    """Ingest ``/roster/{TEAM}/current`` into dim_players. Returns count."""
    abbr = team_abbr.upper()
    payload = fetcher(f"{NHL_API_BASE}/roster/{abbr}/current")

    count = 0
    for group in ("forwards", "defensemen", "goalies"):
        for player in payload.get(group) or []:
            full_name = _roster_full_name(player)
            if not full_name:
                continue
            db.upsert_player(
                conn,
                int(player["id"]),
                full_name,
                normalize_nhl_position(player.get("positionCode")),
                canonical_name=normalize_name(full_name),
                shoots_catches=player.get("shootsCatches"),
                birth_date=player.get("birthDate"),
            )
            count += 1
    if commit:
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Boxscores
# ---------------------------------------------------------------------------


def _ensure_player(
    conn: sqlite3.Connection,
    player_id: int,
    display_name: str,
    position: str,
) -> None:
    """Insert a dim_players row only when missing.

    Boxscores only carry short names ("C. McDavid"); once a full name is
    seeded by roster ingest we never downgrade its canonical_name.
    """
    exists = conn.execute(
        "SELECT 1 FROM dim_players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if not exists:
        db.upsert_player(
            conn,
            player_id,
            display_name,
            position,
            canonical_name=normalize_name(display_name),
        )


def _existing_team_venue(conn: sqlite3.Connection, team_id: int) -> Optional[str]:
    row = conn.execute(
        "SELECT venue FROM dim_teams WHERE team_id = ?", (team_id,)
    ).fetchone()
    return row["venue"] if row else None


def _upsert_game_teams(
    conn: sqlite3.Connection,
    home: Dict[str, Any],
    away: Dict[str, Any],
    venue: str,
) -> None:
    for team, is_home in ((home, True), (away, False)):
        abbr = str(team.get("abbrev") or "")
        if not abbr:
            continue
        # The game venue is only the HOME team's arena; for the away team
        # keep a previously-known venue rather than clobbering it.
        team_venue = (
            venue if is_home else (_existing_team_venue(conn, int(team["id"])) or "")
        )
        db.upsert_team(
            conn,
            int(team["id"]),
            abbr,
            _team_full_name(team),
            team_venue,
            altitude_ft=TEAM_ALTITUDE_FT.get(abbr, 0),
        )


def ingest_boxscore(
    conn: sqlite3.Connection,
    game_id: int,
    *,
    fetcher: JsonFetcher = default_json_fetcher,
    advanced_metrics: Optional[Dict[int, Dict[str, float]]] = None,
    commit: bool = True,
) -> Tuple[int, int]:
    """Ingest ``/gamecenter/{game_id}/boxscore`` into the fact tables.

    ``advanced_metrics`` maps ``player_id`` -> MoneyPuck-derived fields
    (``ixg``, ``icorsi``, ``ihdcf``, ``on_ice_xg_share`` for skaters;
    ``gsax``, ``hd_save_pct`` for goalies). Returns
    ``(skaters_inserted, goalies_inserted)``.
    """
    metrics = advanced_metrics or {}
    payload = fetcher(f"{NHL_API_BASE}/gamecenter/{game_id}/boxscore")

    home = payload.get("homeTeam") or {}
    away = payload.get("awayTeam") or {}
    game_date = str(
        payload.get("gameDate") or str(payload.get("startTimeUTC") or "")[:10]
    )
    season = int(payload.get("season") or str(game_id)[:4])
    venue = _local_name(payload.get("venue"))

    _upsert_game_teams(conn, home, away, venue)

    home_id = int(home["id"])
    away_id = int(away["id"])
    # Rest days are computed BEFORE today's rows are inserted so the
    # current game cannot count as its own "previous game".
    rest_by_team = {
        home_id: compute_rest_days(conn, home_id, game_date),
        away_id: compute_rest_days(conn, away_id, game_date),
    }

    stats = payload.get("playerByGameStats") or {}
    skaters = 0
    goalies = 0

    for side_key, team, opponent_id, is_home in (
        ("awayTeam", away, home_id, 0),
        ("homeTeam", home, away_id, 1),
    ):
        team_id = int(
            team.get("id") or (away_id if side_key == "awayTeam" else home_id)
        )
        side = stats.get(side_key) or {}

        for group in ("forwards", "defense"):
            for p in side.get(group) or []:
                pid = int(p["playerId"])
                name = _local_name(p.get("name")) or str(pid)
                _ensure_player(
                    conn, pid, name, normalize_nhl_position(p.get("position"))
                )
                adv = metrics.get(pid, {})
                db.insert_skater_game(
                    conn,
                    {
                        "game_id": game_id,
                        "player_id": pid,
                        "season": season,
                        "game_date": game_date,
                        "team_id": team_id,
                        "opponent_id": opponent_id,
                        "is_home": is_home,
                        "rest_days": rest_by_team[team_id],
                        "toi_seconds": parse_toi_to_seconds(p.get("toi")),
                        "pp_toi_seconds": parse_toi_to_seconds(
                            p.get("powerPlayToi") or "0:00"
                        ),
                        "goals": int(p.get("goals") or 0),
                        "assists": int(p.get("assists") or 0),
                        "shots_on_goal": int(p.get("sog") or p.get("shots") or 0),
                        "blocks": int(p.get("blockedShots") or p.get("blocks") or 0),
                        "hits": int(p.get("hits") or 0),
                        "faceoffs_won": int(
                            p.get("faceoffsWon") or p.get("faceoffWins") or 0
                        ),
                        "pim": int(p.get("pim") or 0),
                        "ixg": float(adv.get("ixg") or 0.0),
                        "icorsi": int(adv.get("icorsi") or 0),
                        "ihdcf": int(adv.get("ihdcf") or 0),
                        "on_ice_xg_share": float(adv.get("on_ice_xg_share") or 0.0),
                    },
                )
                skaters += 1

        for p in side.get("goalies") or []:
            toi_seconds = parse_toi_to_seconds(p.get("toi"))
            if toi_seconds == 0:
                continue  # dressed backup never saw the ice
            pid = int(p["playerId"])
            name = _local_name(p.get("name")) or str(pid)
            _ensure_player(conn, pid, name, "G")

            shots_against = int(p.get("shotsAgainst") or 0)
            goals_against = int(p.get("goalsAgainst") or 0)
            saves_raw = p.get("saves")
            saves = (
                int(saves_raw)
                if isinstance(saves_raw, (int, float))
                else max(0, shots_against - goals_against)
            )
            adv = metrics.get(pid, {})
            db.insert_goalie_game(
                conn,
                {
                    "game_id": game_id,
                    "goalie_id": pid,
                    "season": season,
                    "game_date": game_date,
                    "team_id": team_id,
                    "opponent_id": opponent_id,
                    "is_home": is_home,
                    "decision": p.get("decision"),
                    "toi_seconds": toi_seconds,
                    "shots_against": shots_against,
                    "goals_against": goals_against,
                    "saves": saves,
                    "gsax": float(adv.get("gsax") or 0.0),
                    "hd_save_pct": (
                        float(adv["hd_save_pct"])
                        if adv.get("hd_save_pct") is not None
                        else None
                    ),
                },
            )
            goalies += 1

    if commit:
        conn.commit()
    return skaters, goalies
