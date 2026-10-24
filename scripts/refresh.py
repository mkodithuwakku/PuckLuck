"""Daily refresh entrypoint + allowlisted ``dist/report.json`` export.

The static report is pushed to GitHub Pages — a PUBLIC surface — so the
export enforces a strict key allowlist and hard-rejects anything that
looks like a secret or a local filesystem path (SRS §6.2/§7.1). A report
that fails validation is never written.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

import db
from betting.ev import (
    MarketTotalLine,
    american_to_decimal,
    compute_ev,
    evaluate_total_market,
    upsert_total_line,
)
from betting.poisson import (
    build_matchup_inputs_from_db,
    compute_expected_goals,
    poisson_pmf_grid,
    price_total_goals,
)
from fantasy.solver import (
    CandidatePlayer,
    SwapEvaluation,
    evaluate_swap,
    solve_best_stream,
)
from features.builder import FeatureBuilder
from models.ridge import ComponentRidgeModel, ScoringRescorer

STALE_AFTER_HOURS: float = 26.0
DEFAULT_REPORT_PATH = Path("dist/report.json")

TOP_LEVEL_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "generated_at",
        "stale_after_hours",
        "team_summary",
        "roster",
        "recommendation",
        "games",
        "free_agents",
    }
)
TEAM_SUMMARY_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "team_name",
        "team_initials",
        "league_name",
        "adds_used",
        "adds_limit",
        "matchup_score",
    }
)
ROSTER_ITEM_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "player_id",
        "display_name",
        "position",
        "selected_slot",
        "team_abbr",
        "opponent_abbr",
        "opp_def_rtg_rel",
        "goalie_status",
        "projected_fp",
        "shots_on_goal",
        "ixg",
        "goals",
        "assists",
        "blocks",
        "hits",
    }
)
GAME_ITEM_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "game_id",
        "start_time_utc",
        "home_abbr",
        "away_abbr",
        "status",
        "lambda_home",
        "lambda_away",
        "expected_total",
        "line",
        "over_odds",
        "under_odds",
        "prob_over",
        "prob_under",
        "best_side",
        "ev",
        "is_positive_ev",
        "home_goalie_status",
        "away_goalie_status",
    }
)
FREE_AGENT_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "player_id",
        "display_name",
        "position",
        "team_abbr",
        "percent_owned",
        "projected_fp",
        "ixg_per60_ewma",
        "icorsi_per60_ewma",
        "pp_toi_share_ewma",
    }
)
RECOMMENDATION_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "action",
        "explanation",
        "delta_value",
        "tonight_gain",
        "short_term_gain",
        "asset_depreciation_penalty",
        "horizon_weeks",
        "streamer_name",
        "drop_name",
    }
)

FORBIDDEN_KEY_SUBSTRINGS: Tuple[str, ...] = (
    "secret",
    "token",
    "password",
    "client_id",
    "authorization",
    "api_key",
    "env_path",
    "db_path",
)
FORBIDDEN_VALUE_SUBSTRINGS: Tuple[str, ...] = (
    "/Users/",
    "/home/",
    ".sqlite3",
    ".env",
    "Bearer ",
    "Basic ",
)

_ALLOWED_KEYS_ALL: FrozenSet[str] = frozenset(
    TOP_LEVEL_ALLOWLIST
    | TEAM_SUMMARY_ALLOWLIST
    | ROSTER_ITEM_ALLOWLIST
    | GAME_ITEM_ALLOWLIST
    | FREE_AGENT_ALLOWLIST
    | RECOMMENDATION_ALLOWLIST
)


def _filter_dict(raw: Any, allowlist: FrozenSet[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in allowlist}


def _filter_items(raw: Any, allowlist: FrozenSet[str]) -> list:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [_filter_dict(item, allowlist) for item in raw]


def sanitize_public_report(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Strip every key outside the per-section allowlists."""
    report: Dict[str, Any] = {}
    for key in ("generated_at", "stale_after_hours"):
        if key in raw:
            report[key] = raw[key]
    if "team_summary" in raw:
        report["team_summary"] = _filter_dict(
            raw["team_summary"], TEAM_SUMMARY_ALLOWLIST
        )
    if "roster" in raw:
        report["roster"] = _filter_items(raw["roster"], ROSTER_ITEM_ALLOWLIST)
    if "recommendation" in raw:
        report["recommendation"] = _filter_dict(
            raw["recommendation"], RECOMMENDATION_ALLOWLIST
        )
    if "games" in raw:
        report["games"] = _filter_items(raw["games"], GAME_ITEM_ALLOWLIST)
    if "free_agents" in raw:
        report["free_agents"] = _filter_items(raw["free_agents"], FREE_AGENT_ALLOWLIST)
    return report


def _walk_forbidden(node: Any) -> None:
    """Raise ValueError on forbidden key substrings or leaked values."""
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(s in lowered for s in FORBIDDEN_KEY_SUBSTRINGS):
                raise ValueError(f"forbidden key in report: {key!r}")
            if key not in _ALLOWED_KEYS_ALL:
                raise ValueError(f"non-allowlisted key in report: {key!r}")
            _walk_forbidden(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk_forbidden(item)
    elif isinstance(node, str):
        if any(s in node for s in FORBIDDEN_VALUE_SUBSTRINGS):
            raise ValueError("report value resembles a local path or secret")


def validate_public_report(report: Dict[str, Any]) -> None:
    """Enforce the §6.2 static security boundary; raises ValueError."""
    if not isinstance(report, dict):
        raise ValueError("report must be a dict")
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise ValueError("report missing non-empty 'generated_at'")
    for key in report:
        if key not in TOP_LEVEL_ALLOWLIST:
            raise ValueError(f"non-allowlisted top-level key: {key!r}")
    for key in report.get("team_summary") or {}:
        if key not in TEAM_SUMMARY_ALLOWLIST:
            raise ValueError(f"non-allowlisted team_summary key: {key!r}")
    for item in report.get("roster") or []:
        for key in item:
            if key not in ROSTER_ITEM_ALLOWLIST:
                raise ValueError(f"non-allowlisted roster key: {key!r}")
    for key in report.get("recommendation") or {}:
        if key not in RECOMMENDATION_ALLOWLIST:
            raise ValueError(f"non-allowlisted recommendation key: {key!r}")
    for item in report.get("games") or []:
        for key in item:
            if key not in GAME_ITEM_ALLOWLIST:
                raise ValueError(f"non-allowlisted game key: {key!r}")
    for item in report.get("free_agents") or []:
        for key in item:
            if key not in FREE_AGENT_ALLOWLIST:
                raise ValueError(f"non-allowlisted free_agents key: {key!r}")
    _walk_forbidden(report)


def _recommendation_dict(
    rec: Union[SwapEvaluation, Dict[str, Any]],
) -> Dict[str, Any]:
    if isinstance(rec, SwapEvaluation):
        return {
            "action": rec.action,
            "explanation": rec.explanation,
            "delta_value": rec.delta_value,
            "tonight_gain": rec.tonight_gain,
            "short_term_gain": rec.short_term_gain,
            "asset_depreciation_penalty": rec.asset_depreciation_penalty,
            "horizon_weeks": rec.horizon_weeks,
            "streamer_name": rec.streamer.display_name,
            "drop_name": rec.drop_candidate.display_name,
        }
    return _filter_dict(rec, RECOMMENDATION_ALLOWLIST)


def build_public_report(
    *,
    generated_at: Optional[str] = None,
    team_summary: Optional[Dict[str, Any]] = None,
    roster: Sequence[Dict[str, Any]] = (),
    recommendation: Optional[Union[SwapEvaluation, Dict[str, Any]]] = None,
    games: Sequence[Dict[str, Any]] = (),
    free_agents: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Assemble, sanitize, and validate the public report payload."""
    raw: Dict[str, Any] = {
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stale_after_hours": STALE_AFTER_HOURS,
        "team_summary": team_summary or {},
        "roster": list(roster),
        "games": list(games),
        "free_agents": list(free_agents),
    }
    if recommendation is not None:
        raw["recommendation"] = _recommendation_dict(recommendation)

    report = sanitize_public_report(raw)
    validate_public_report(report)
    return report


def write_report_json(
    report: Dict[str, Any],
    path: Union[str, Path] = DEFAULT_REPORT_PATH,
) -> Path:
    """Atomically write a validated report; refuses invalid payloads."""
    validate_public_report(report)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------------------
# Phase 6: warehouse -> report pipeline + Yahoo-optional demo mode
# ---------------------------------------------------------------------------

DEFAULT_STAT_WEIGHTS: Dict[str, float] = {
    "G": 3.0,
    "A": 2.0,
    "SOG": 0.4,
    "HIT": 0.5,
    "BLK": 0.5,
}

_DEMO_TEAMS: Tuple[Tuple[int, str, str, str, int], ...] = (
    (25, "EDM", "Edmonton Oilers", "Rogers Place", 2208),
    (21, "COL", "Colorado Avalanche", "Ball Arena", 5280),
    (10, "TOR", "Toronto Maple Leafs", "Scotiabank Arena", 0),
    (12, "CAR", "Carolina Hurricanes", "PNC Arena", 0),
)

# (player_id, display_name, primary_position, team_id, yahoo_player_id)
_DEMO_SKATERS: Tuple[Tuple[int, str, str, int, Optional[str]], ...] = (
    (8478402, "Connor McDavid", "C", 25, "449.p.6743"),
    (8480803, "Evan Bouchard", "D", 25, "449.p.8038"),
    (8475786, "Zach Hyman", "LW", 25, None),
    (8477492, "Nathan MacKinnon", "C", 21, "449.p.7492"),
    (8478038, "Devon Toews", "D", 21, None),
    (8480065, "Cale Makar", "D", 21, "449.p.8065"),
    (8479318, "Auston Matthews", "C", 10, "449.p.9318"),
    (8477939, "William Nylander", "RW", 10, None),
    (8478427, "Sebastian Aho", "C", 12, None),
    (8480830, "Andrei Svechnikov", "LW", 12, None),
    (8482093, "Seth Jarvis", "RW", 12, None),
)
_DEMO_GOALIES: Tuple[Tuple[int, str, int, Optional[str]], ...] = (
    (8479979, "Stuart Skinner", 25, "449.p.7979"),
    (8480382, "Alexandar Georgiev", 21, None),
)

_DEMO_DATES: Tuple[str, str, str] = ("2026-10-15", "2026-10-18", "2026-10-21")

# game_id -> (home_team_id, away_team_id) for tonight's slate.
_DEMO_MATCHUPS: Dict[int, Tuple[int, int]] = {
    2026020101: (25, 21),  # COL @ EDM
    2026020102: (10, 12),  # CAR @ TOR
}

# Home team for each historical date per matchup pair.
_DEMO_HOME_BY_DATE: Dict[str, FrozenSet[int]] = {
    "2026-10-15": frozenset({25, 10}),
    "2026-10-18": frozenset({21, 12}),
    "2026-10-21": frozenset({25, 10}),
}
_DEMO_OPPONENT: Dict[int, int] = {25: 21, 21: 25, 10: 12, 12: 10}
_DEMO_GAME_IDS: Dict[Tuple[int, int], List[int]] = {
    (21, 25): [2026101501, 2026101801, 2026102101],
    (10, 12): [2026101502, 2026101802, 2026102102],
}


def _skater_line(
    toi: int,
    pp_toi: int,
    goals: int,
    assists: int,
    sog: int,
    hits: int,
    blocks: int,
    ixg: float,
    icorsi: float,
    ihdcf: float,
) -> Dict[str, Any]:
    return {
        "toi_seconds": toi,
        "pp_toi_seconds": pp_toi,
        "goals": goals,
        "assists": assists,
        "shots_on_goal": sog,
        "hits": hits,
        "blocks": blocks,
        "ixg": ixg,
        "icorsi": icorsi,
        "ihdcf": ihdcf,
    }


# Three strictly-prior game lines per demo skater.
_DEMO_SKATER_GAMES: Dict[int, Tuple[Dict[str, Any], ...]] = {
    8478402: (
        _skater_line(1350, 220, 2, 1, 5, 1, 0, 1.10, 13.5, 4.2),
        _skater_line(1280, 210, 1, 2, 4, 0, 1, 0.85, 11.0, 3.5),
        _skater_line(1390, 230, 1, 1, 6, 1, 0, 1.25, 14.0, 4.8),
    ),
    8480803: (
        _skater_line(1420, 260, 0, 1, 3, 1, 2, 0.45, 8.5, 1.5),
        _skater_line(1380, 255, 0, 2, 2, 0, 3, 0.35, 7.0, 1.2),
        _skater_line(1440, 270, 1, 0, 4, 1, 2, 0.55, 9.0, 1.8),
    ),
    8475786: (
        _skater_line(1110, 160, 1, 0, 4, 3, 0, 0.80, 10.0, 3.0),
        _skater_line(1080, 150, 1, 1, 3, 2, 1, 0.70, 9.0, 2.6),
        _skater_line(1140, 170, 0, 1, 5, 3, 0, 0.95, 10.5, 3.4),
    ),
    8477492: (
        _skater_line(1320, 215, 1, 2, 5, 2, 0, 1.00, 12.5, 4.0),
        _skater_line(1290, 205, 0, 1, 4, 1, 1, 0.80, 11.0, 3.2),
        _skater_line(1350, 220, 2, 1, 6, 2, 0, 1.15, 13.0, 4.5),
    ),
    8478038: (
        _skater_line(1370, 60, 0, 1, 2, 1, 3, 0.35, 7.5, 1.0),
        _skater_line(1340, 55, 0, 0, 2, 2, 2, 0.30, 7.0, 0.8),
        _skater_line(1390, 65, 0, 1, 3, 1, 3, 0.40, 8.0, 1.1),
    ),
    8479318: (
        _skater_line(1260, 200, 2, 0, 6, 1, 0, 1.15, 11.0, 3.8),
        _skater_line(1220, 195, 1, 1, 5, 0, 0, 0.95, 10.0, 3.4),
        _skater_line(1290, 210, 1, 0, 7, 1, 0, 1.30, 12.0, 4.2),
    ),
    8480065: (
        _skater_line(1500, 180, 0, 1, 3, 1, 2, 0.85, 9.5, 1.6),
        _skater_line(1460, 175, 1, 0, 4, 1, 2, 0.95, 10.0, 1.9),
        _skater_line(1520, 185, 0, 1, 2, 1, 2, 0.75, 8.5, 1.4),
    ),
    8477939: (
        _skater_line(1180, 190, 1, 1, 4, 1, 0, 0.85, 10.0, 2.6),
        _skater_line(1150, 185, 0, 0, 3, 0, 0, 0.70, 9.0, 2.0),
        _skater_line(1210, 195, 1, 1, 5, 1, 1, 0.95, 10.5, 2.9),
    ),
    8478427: (
        _skater_line(1200, 140, 1, 1, 4, 1, 0, 0.90, 10.5, 2.8),
        _skater_line(1170, 135, 0, 1, 3, 0, 1, 0.75, 9.5, 2.2),
        _skater_line(1230, 145, 1, 0, 4, 1, 1, 0.95, 11.0, 3.0),
    ),
    8480830: (
        _skater_line(1150, 130, 0, 0, 3, 2, 0, 0.65, 9.0, 2.0),
        _skater_line(1120, 125, 1, 1, 4, 3, 1, 0.80, 10.0, 2.5),
        _skater_line(1180, 135, 0, 1, 3, 2, 0, 0.60, 8.5, 1.8),
    ),
    8482093: (
        _skater_line(1060, 120, 1, 1, 3, 2, 1, 0.60, 9.0, 2.2),
        _skater_line(1030, 115, 0, 1, 2, 1, 1, 0.50, 8.0, 1.8),
        _skater_line(1090, 125, 1, 0, 4, 2, 0, 0.70, 9.5, 2.5),
    ),
}


def _goalie_line(
    toi: int,
    shots_against: int,
    goals_against: int,
    gsax: float,
    hd_save_pct: float,
) -> Dict[str, Any]:
    return {
        "toi_seconds": toi,
        "shots_against": shots_against,
        "saves": shots_against - goals_against,
        "goals_against": goals_against,
        "gsax": gsax,
        "hd_save_pct": hd_save_pct,
    }


_DEMO_GOALIE_GAMES: Dict[int, Tuple[Dict[str, Any], ...]] = {
    8479979: (
        _goalie_line(3600, 28, 2, 0.80, 0.87),
        _goalie_line(3600, 33, 4, -1.10, 0.79),
        _goalie_line(3600, 25, 1, 1.30, 0.90),
    ),
    8480382: (
        _goalie_line(3600, 30, 3, -0.40, 0.82),
        _goalie_line(3600, 27, 2, 0.60, 0.85),
        _goalie_line(3600, 34, 4, -1.50, 0.78),
    ),
}

# (game_id, line, over_odds, under_odds, captured_at)
_DEMO_MARKET_LINES: Tuple[Tuple[int, float, int, int, str], ...] = (
    (2026020101, 6.5, -110, -110, "2026-10-22T18:00:00Z"),
    (2026020101, 6.5, -105, -115, "2026-10-22T19:00:00Z"),
    (2026020102, 6.0, -110, -110, "2026-10-22T18:00:00Z"),
    (2026020102, 6.0, -120, 100, "2026-10-22T19:00:00Z"),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Return actual column names for ``table`` (schema-driven seeding)."""
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def _pick(columns: Sequence[str], *aliases: str) -> Optional[str]:
    for alias in aliases:
        if alias in columns:
            return alias
    return None


def _insert_fact_rows(
    conn: sqlite3.Connection, table: str, rows: Sequence[Dict[str, Any]]
) -> None:
    """INSERT OR REPLACE ``rows`` filtered to ``table``'s actual columns."""
    columns = _table_columns(conn, table)
    colset = set(columns)
    for row in rows:
        filtered = {k: v for k, v in row.items() if k in colset}
        if not filtered:
            continue
        cols = list(filtered)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(filtered[c] for c in cols),
        )


def _make_market_line(
    game_id: int, total: float, over: int, under: int, captured_at: str
) -> MarketTotalLine:
    """Construct MarketTotalLine by matching the dataclass's real fields."""
    candidates: Dict[str, Any] = {
        "game_id": game_id,
        "sportsbook": "PuckLuck Demo",
        "book": "PuckLuck Demo",
        "book_name": "PuckLuck Demo",
        "line": total,
        "total": total,
        "total_line": total,
        "over_odds": over,
        "over_price": over,
        "under_odds": under,
        "under_price": under,
        "captured_at": captured_at,
        "timestamp": captured_at,
        "as_of": captured_at,
        "target_id": None,
        "market": "total",
        "market_type": "total",
    }
    params = inspect.signature(MarketTotalLine).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return MarketTotalLine(**candidates)
    kwargs = {k: v for k, v in candidates.items() if k in params}
    missing = [
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and name not in kwargs
        and p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if missing:
        raise TypeError(f"MarketTotalLine required params not covered: {missing}")
    return MarketTotalLine(**kwargs)


def _make_candidate(
    player_id: int,
    name: str,
    fp_tonight: float,
    fp_remaining_week: Tuple[float, ...],
    ros_weekly: float,
    droppable: bool,
) -> CandidatePlayer:
    candidates: Dict[str, Any] = {
        "player_id": player_id,
        "display_name": name,
        "name": name,
        "fp_tonight": fp_tonight,
        "projected_fp": fp_tonight,
        "fp_remaining_week": fp_remaining_week,
        "fp_ros_weekly_avg": ros_weekly,
        "ros_weekly_avg": ros_weekly,
        "is_droppable": droppable,
        "droppable": droppable,
        "can_drop": droppable,
    }
    params = inspect.signature(CandidatePlayer).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return CandidatePlayer(**candidates)
    kwargs = {k: v for k, v in candidates.items() if k in params}
    missing = [
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and name not in kwargs
        and p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if missing:
        raise TypeError(f"CandidatePlayer required params not covered: {missing}")
    return CandidatePlayer(**kwargs)


def seed_demo_warehouse(
    conn: sqlite3.Connection, *, as_of_date: str = "2026-10-23"
) -> None:
    """Deterministically populate the warehouse for offline/demo runs."""
    for team_id, abbr, name, venue, altitude in _DEMO_TEAMS:
        db.upsert_team(conn, team_id, abbr, name, venue, altitude_ft=altitude)

    for player_id, name, position, _team_id, yahoo_id in _DEMO_SKATERS:
        db.upsert_player(conn, player_id, name, position, yahoo_player_id=yahoo_id)
    for player_id, name, team_id, yahoo_id in _DEMO_GOALIES:
        db.upsert_player(conn, player_id, name, "G", yahoo_player_id=yahoo_id)

    skater_rows: List[Dict[str, Any]] = []
    for player_id, _name, _pos, team_id, _y in _DEMO_SKATERS:
        opponent_id = _DEMO_OPPONENT[team_id]
        pair = tuple(sorted((team_id, opponent_id)))
        for date, line in zip(_DEMO_DATES, _DEMO_SKATER_GAMES[player_id]):
            gid = _DEMO_GAME_IDS[pair][_DEMO_DATES.index(date)]
            skater_rows.append(
                {
                    "game_id": gid,
                    "player_id": player_id,
                    "team_id": team_id,
                    "opponent_id": opponent_id,
                    "game_date": date,
                    "season": "20262027",
                    "rest_days": 2,
                    "home": int(team_id in _DEMO_HOME_BY_DATE[date]),
                    "is_home": int(team_id in _DEMO_HOME_BY_DATE[date]),
                    "home_flag": int(team_id in _DEMO_HOME_BY_DATE[date]),
                    **line,
                }
            )
    _insert_fact_rows(conn, "fact_skater_games", skater_rows)

    goalie_rows: List[Dict[str, Any]] = []
    for player_id, _name, team_id, _y in _DEMO_GOALIES:
        opponent_id = _DEMO_OPPONENT[team_id]
        pair = tuple(sorted((team_id, opponent_id)))
        for date, line in zip(_DEMO_DATES, _DEMO_GOALIE_GAMES[player_id]):
            gid = _DEMO_GAME_IDS[pair][_DEMO_DATES.index(date)]
            goalie_rows.append(
                {
                    "game_id": gid,
                    "player_id": player_id,
                    "goalie_id": player_id,
                    "team_id": team_id,
                    "opponent_id": opponent_id,
                    "game_date": date,
                    "season": "20262027",
                    "rest_days": 2,
                    "home": int(team_id in _DEMO_HOME_BY_DATE[date]),
                    "is_home": int(team_id in _DEMO_HOME_BY_DATE[date]),
                    "home_flag": int(team_id in _DEMO_HOME_BY_DATE[date]),
                    **line,
                }
            )
    _insert_fact_rows(conn, "fact_goalie_games", goalie_rows)

    for game_id, total, over, under, captured_at in _DEMO_MARKET_LINES:
        upsert_total_line(
            conn, _make_market_line(game_id, total, over, under, captured_at)
        )

    conn.commit()


def _team_abbrs(conn: sqlite3.Connection) -> Dict[int, str]:
    columns = _table_columns(conn, "dim_teams")
    id_col = _pick(columns, "team_id", "id")
    abbr_col = _pick(columns, "abbreviation", "abbr", "team_abbr", "tricode")
    if id_col is None or abbr_col is None:
        return {}
    rows = conn.execute(f"SELECT {id_col}, {abbr_col} FROM dim_teams").fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


def _latest_by_player(
    conn: sqlite3.Connection, table: str, as_of_date: str
) -> Dict[int, Tuple[int, int]]:
    """player_id -> (team_id, opponent_id) from their most recent game."""
    id_col = _pick(_table_columns(conn, table), "player_id", "goalie_id")
    rows = conn.execute(
        f"SELECT {id_col}, team_id, opponent_id FROM {table} "
        "WHERE game_date < ? ORDER BY game_date DESC, game_id DESC",
        (as_of_date,),
    ).fetchall()
    latest: Dict[int, Tuple[int, int]] = {}
    for player_id, team_id, opponent_id in rows:
        latest.setdefault(int(player_id), (int(team_id), int(opponent_id)))
    return latest


def _team_goalie(
    conn: sqlite3.Connection, team_id: int, as_of_date: str
) -> Optional[int]:
    row = conn.execute(
        "SELECT goalie_id FROM fact_goalie_games "
        "WHERE team_id = ? AND game_date < ? "
        "ORDER BY game_date DESC, game_id DESC LIMIT 1",
        (team_id, as_of_date),
    ).fetchone()
    return int(row[0]) if row else None


def _fallback_components(
    conn: sqlite3.Connection, player_id: int, as_of_date: str
) -> Dict[str, float]:
    """Unfitted-model fallback: components from historical game averages."""
    row = conn.execute(
        "SELECT AVG(goals), AVG(assists), AVG(shots_on_goal), "
        "AVG(hits), AVG(blocks), AVG(ixg) FROM fact_skater_games "
        "WHERE player_id = ? AND game_date < ?",
        (player_id, as_of_date),
    ).fetchone()
    goals, assists, sog, hits, blocks, ixg = row or (0,) * 6
    return {
        "SOG": float(sog or 0.0),
        "iXG": float(ixg or 0.0),
        "G": float(goals or 0.0),
        "A": float(assists or 0.0),
        "BLK": float(blocks or 0.0),
        "HIT": float(hits or 0.0),
    }


def _lambdas(expected_goals: Any) -> Tuple[float, float]:
    if hasattr(expected_goals, "lambda_home"):
        return float(expected_goals.lambda_home), float(expected_goals.lambda_away)
    if isinstance(expected_goals, dict):
        return float(expected_goals["lambda_home"]), float(
            expected_goals["lambda_away"]
        )
    home, away = expected_goals
    return float(home), float(away)


def _price_total(lam_home: float, lam_away: float, line: float) -> Any:
    params = inspect.signature(price_total_goals).parameters
    if len(params) >= 3:
        return price_total_goals(lam_home, lam_away, line)
    return price_total_goals(poisson_pmf_grid(lam_home, lam_away), line)


def _market_game_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """One consensus-ish row per distinct game_id in fact_market_lines."""
    columns = _table_columns(conn, "fact_market_lines")
    gid_col = _pick(columns, "game_id")
    line_col = _pick(columns, "line", "total_line", "total")
    over_col = _pick(columns, "over_odds", "over_price")
    under_col = _pick(columns, "under_odds", "under_price")
    if not all((gid_col, line_col, over_col, under_col)):
        return []
    rows = [
        dict(zip(columns, r))
        for r in conn.execute("SELECT * FROM fact_market_lines").fetchall()
    ]
    by_game: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        by_game.setdefault(int(row[gid_col]), []).append(row)
    consensus = []
    for gid in sorted(by_game):
        group = by_game[gid]
        lines = sorted(float(g[line_col]) for g in group)
        mid = lines[len(lines) // 2] if len(lines) % 2 else lines[len(lines) // 2 - 1]
        consensus.append(
            {
                "game_id": gid,
                "line": mid,
                "over_odds": max(int(g[over_col]) for g in group),
                "under_odds": max(int(g[under_col]) for g in group),
            }
        )
    return consensus


def _recent_pair_stream(
    conn: sqlite3.Connection, as_of_date: str
) -> List[Tuple[int, int]]:
    """Most-recent unordered (team, opponent) pairs for matchup fallback."""
    rows = conn.execute(
        "SELECT team_id, opponent_id FROM fact_skater_games "
        "WHERE game_date < ? ORDER BY game_date DESC, game_id DESC",
        (as_of_date,),
    ).fetchall()
    seen: List[Tuple[int, int]] = []
    keys = set()
    for team_id, opponent_id in rows:
        key = frozenset((int(team_id), int(opponent_id)))
        if key not in keys:
            keys.add(key)
            seen.append((int(team_id), int(opponent_id)))
    return seen


def run_warehouse_pipeline(
    conn: sqlite3.Connection,
    *,
    as_of_date: str = "2026-10-23",
    stat_weights: Optional[Dict[str, float]] = None,
    team_summary: Optional[Dict[str, Any]] = None,
    horizon_weeks: float = 2.5,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Run FeatureBuilder -> Ridge -> Rescorer -> Solver/Poisson -> report."""
    weights = stat_weights or DEFAULT_STAT_WEIGHTS
    rescorer = ScoringRescorer(weights)

    fb = FeatureBuilder(conn)
    X, y_by_comp, _sample_keys = fb.build_training_dataset(
        as_of_date, min_prior_games=1
    )
    model = (
        ComponentRidgeModel(l2_penalty=1.0).fit(X, y_by_comp)
        if X.shape[0] >= 2
        else None
    )

    player_cols = _table_columns(conn, "dim_players")
    pos_col = _pick(player_cols, "primary_position", "position")
    yahoo_col = _pick(player_cols, "yahoo_player_id")
    name_col = _pick(player_cols, "display_name", "name")
    players = conn.execute(
        f"SELECT player_id, {name_col}, {pos_col}, {yahoo_col} "
        "FROM dim_players ORDER BY player_id"
    ).fetchall()
    abbrs = _team_abbrs(conn)
    latest_skater = _latest_by_player(conn, "fact_skater_games", as_of_date)
    latest_goalie = _latest_by_player(conn, "fact_goalie_games", as_of_date)

    roster_items: List[Dict[str, Any]] = []
    fa_items: List[Dict[str, Any]] = []
    roster_candidates: List[CandidatePlayer] = []
    fa_candidates: List[CandidatePlayer] = []
    goalie_roster_items: List[Dict[str, Any]] = []

    for player_id, display_name, position, yahoo_id in players:
        player_id = int(player_id)
        if position == "G":
            if yahoo_id is not None and player_id in latest_goalie:
                team_id, opponent_id = latest_goalie[player_id]
                avg = conn.execute(
                    "SELECT AVG(saves), AVG(gsax) FROM fact_goalie_games "
                    "WHERE goalie_id = ? AND game_date < ?",
                    (player_id, as_of_date),
                ).fetchone()
                saves_avg, gsax_avg = avg if avg else (0.0, 0.0)
                proj = max(
                    0.0,
                    round(0.2 * float(saves_avg or 0) + 2.0 * float(gsax_avg or 0), 2),
                )
                goalie_roster_items.append(
                    {
                        "player_id": player_id,
                        "display_name": display_name,
                        "position": "G",
                        "selected_slot": "G",
                        "team_abbr": abbrs.get(team_id),
                        "opponent_abbr": abbrs.get(opponent_id),
                        "goalie_status": "CONFIRMED",
                        "projected_fp": proj,
                        "shots_on_goal": 0,
                    }
                )
            continue
        if player_id not in latest_skater:
            continue
        team_id, opponent_id = latest_skater[player_id]
        fv = fb.build_player_features(
            player_id,
            team_id,
            opponent_id,
            as_of_date,
            is_home=True,
            opponent_goalie_id=_team_goalie(conn, opponent_id, as_of_date),
            team_rest=2,
            opp_rest=2,
        )
        if model is not None:
            comps = model.predict_components(fv.to_array())
        else:
            comps = _fallback_components(conn, player_id, as_of_date)
        comps = {k: round(float(v), 2) for k, v in comps.items()}
        proj_fp = round(float(rescorer.score(comps)), 2)
        feats = fv.features
        ros_weekly = proj_fp * 3.0  # ~3 games per fantasy week

        if yahoo_id is not None:
            roster_items.append(
                {
                    "player_id": player_id,
                    "display_name": display_name,
                    "position": position,
                    "selected_slot": position,
                    "team_abbr": abbrs.get(team_id),
                    "opponent_abbr": abbrs.get(opponent_id),
                    "opp_def_rtg_rel": round(
                        float(feats.get("opp_def_rtg_rel", 0.0)), 3
                    ),
                    "projected_fp": proj_fp,
                    "shots_on_goal": round(comps.get("SOG", 0.0), 1),
                    "ixg": comps.get("iXG", 0.0),
                    "goals": comps.get("G", 0.0),
                    "assists": comps.get("A", 0.0),
                    "blocks": comps.get("BLK", 0.0),
                    "hits": comps.get("HIT", 0.0),
                }
            )
            roster_candidates.append(
                _make_candidate(
                    player_id,
                    display_name,
                    proj_fp,
                    (proj_fp,),
                    ros_weekly,
                    droppable=True,
                )
            )
        else:
            fa_items.append(
                {
                    "player_id": player_id,
                    "display_name": display_name,
                    "position": position,
                    "team_abbr": abbrs.get(team_id),
                    "percent_owned": round(5.0 + (player_id % 40), 1),
                    "projected_fp": proj_fp,
                    "ixg_per60_ewma": round(float(feats.get("ixg_per60_ewma", 0.0)), 3),
                    "icorsi_per60_ewma": round(
                        float(feats.get("icorsi_per60_ewma", 0.0)), 3
                    ),
                    "pp_toi_share_ewma": round(
                        float(feats.get("pp_toi_share_ewma", 0.0)), 3
                    ),
                }
            )
            fa_candidates.append(
                _make_candidate(
                    player_id,
                    display_name,
                    proj_fp,
                    (proj_fp,),
                    ros_weekly,
                    droppable=False,
                )
            )

    roster_items.sort(key=lambda r: -float(r["projected_fp"]))
    roster_items.extend(goalie_roster_items)
    fa_items.sort(key=lambda f: -float(f["ixg_per60_ewma"]))

    recommendation = solve_best_stream(
        fa_candidates, roster_candidates, horizon_weeks=horizon_weeks
    )
    if recommendation is None and fa_candidates and roster_candidates:
        worst = min(roster_candidates, key=lambda c: c.fp_ros_weekly_avg)
        best = max(fa_candidates, key=lambda c: c.fp_tonight)
        recommendation = evaluate_swap(best, worst, horizon_weeks=horizon_weeks)

    game_items = _build_game_items(conn, as_of_date, abbrs)

    summary = team_summary or {
        "team_name": "PuckLuck Demo",
        "team_initials": "PD",
        "league_name": "Demo League",
        "adds_used": 0,
        "adds_limit": 4,
        "matchup_score": "0 - 0",
    }
    return build_public_report(
        generated_at=generated_at,
        team_summary=summary,
        roster=roster_items,
        recommendation=recommendation,
        games=game_items,
        free_agents=fa_items,
    )


def _build_game_items(
    conn: sqlite3.Connection, as_of_date: str, abbrs: Dict[int, str]
) -> List[Dict[str, Any]]:
    consensus = _market_game_rows(conn)
    if not consensus:
        return []
    fallback_pairs = _recent_pair_stream(conn, as_of_date)
    items: List[Dict[str, Any]] = []
    for idx, market in enumerate(consensus):
        game_id = market["game_id"]
        pairing = _DEMO_MATCHUPS.get(game_id)
        if pairing is None and fallback_pairs:
            pairing = fallback_pairs[idx % len(fallback_pairs)]
        if pairing is None:
            continue
        home_id, away_id = pairing

        home_inputs, away_inputs = build_matchup_inputs_from_db(
            conn, home_id, away_id, as_of_date
        )
        lam_home, lam_away = _lambdas(compute_expected_goals(home_inputs, away_inputs))
        price = _price_total(lam_home, lam_away, float(market["line"]))

        line_obj = _make_market_line(
            game_id,
            float(market["line"]),
            int(market["over_odds"]),
            int(market["under_odds"]),
            f"{as_of_date}T18:00:00Z",
        )
        evaluate_total_market(price, line_obj)

        prob_over = float(getattr(price, "prob_over", 0.5))
        prob_under = float(getattr(price, "prob_under", 0.5))
        expected_total = float(getattr(price, "expected_total", lam_home + lam_away))
        ev_over = compute_ev(prob_over, american_to_decimal(int(market["over_odds"])))
        ev_under = compute_ev(
            prob_under, american_to_decimal(int(market["under_odds"]))
        )
        best_side, best_ev = (
            ("OVER", ev_over) if ev_over >= ev_under else ("UNDER", ev_under)
        )
        items.append(
            {
                "game_id": game_id,
                "start_time_utc": f"{as_of_date}T01:00:00Z",
                "home_abbr": abbrs.get(home_id),
                "away_abbr": abbrs.get(away_id),
                "status": "Scheduled",
                "lambda_home": round(lam_home, 2),
                "lambda_away": round(lam_away, 2),
                "expected_total": round(expected_total, 2),
                "line": market["line"],
                "over_odds": int(market["over_odds"]),
                "under_odds": int(market["under_odds"]),
                "prob_over": round(prob_over, 4),
                "prob_under": round(prob_under, 4),
                "best_side": best_side,
                "ev": round(best_ev, 4),
                "is_positive_ev": bool(best_ev >= 0.035),
                "home_goalie_status": "CONFIRMED",
                "away_goalie_status": "PROJECTED",
            }
        )
    return items


def _dim_players_empty(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM dim_players").fetchone()
    return int(row[0]) == 0


def main(argv: Optional[Sequence[str]] = None) -> Path:
    """Refresh entrypoint: warehouse -> engines -> allowlisted report."""
    parser = argparse.ArgumentParser(
        prog="scripts.refresh",
        description="Build the allowlisted PuckLuck report from the warehouse.",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("PUCKLUCK_DB_PATH", "data/puckluck.sqlite3"),
    )
    parser.add_argument("--output", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--date", default="2026-10-23")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Seed the demo warehouse before running the pipeline.",
    )
    args = parser.parse_args(argv)

    conn = db.init_db(args.db_path)
    try:
        if args.demo or _dim_players_empty(conn):
            seed_demo_warehouse(conn, as_of_date=args.date)
        report = run_warehouse_pipeline(conn, as_of_date=args.date)
        path = write_report_json(report, args.output)
    finally:
        conn.close()
    print(f"wrote {path}")
    return path


if __name__ == "__main__":
    main()
