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
import math
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

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
from ingestion.moneypuck import (
    load_goalies,
    load_skaters,
    update_cache,
)
from ingestion.nhl import (
    NHL_API_BASE,
    TEAM_ALTITUDE_FT,
    JsonFetcher,
    ScheduledGame,
    default_json_fetcher,
    fetch_schedule,
    ingest_boxscore,
    ingest_team_roster,
)
from models.backtest import (
    DEFAULT_MODEL_PARAMS_PATH,
    BacktestReport,
    TunedModelParams,
    load_model_params,
    run_walk_forward_backtest,
    save_model_params,
)
from models.ridge import ComponentRidgeModel, ScoringRescorer

STALE_AFTER_HOURS: float = 26.0
DEFAULT_REPORT_PATH = Path("dist/report.json")

TOP_LEVEL_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "generated_at",
        "stale_after_hours",
        "active_slate_date",
        "team_summary",
        "roster",
        "recommendation",
        "games",
        "free_agents",
        "teams",
        "point_scorers",
        "model_health",
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
        "line_source",
        "final_home_score",
        "final_away_score",
        "final_total",
        "lean_hit",
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
POINT_SCORER_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "player_id",
        "display_name",
        "position",
        "team_abbr",
        "opponent_abbr",
        "projected_goals",
        "projected_assists",
        "projected_points",
        "shots_on_goal",
        "ixg_per60_ewma",
        "pp_toi_share_ewma",
        "prob_point",
        "prob_goal",
        "prob_assist",
        "fair_point_odds",
        "fair_goal_odds",
        "fair_assist_odds",
        "market_point_odds",
        "market_goal_odds",
        "market_assist_odds",
        "market_point_src",
        "market_goal_src",
        "market_assist_src",
        "point_ev",
        "goal_ev",
        "assist_ev",
        "best_prop_market",
        "best_prop_odds",
        "best_prop_ev",
        "is_positive_ev",
        "odds_source",
    }
)
FANTASY_TEAM_ENTRY_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "team_key",
        "team_summary",
        "roster",
        "recommendation",
        "waiver_targets",
    }
)
MODEL_HEALTH_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "window_start",
        "window_end",
        "samples_evaluated",
        "games_evaluated",
        "fantasy_point_mae",
        "totals_brier_score",
        "ewma_alpha",
        "ridge_l2_penalty",
        "poisson_base_rate",
        "suggestions",
        "improved_over_baseline",
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
    | POINT_SCORER_ALLOWLIST
    | FANTASY_TEAM_ENTRY_ALLOWLIST
    | MODEL_HEALTH_ALLOWLIST
)


def _filter_dict(raw: Any, allowlist: FrozenSet[str]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in allowlist}


def _filter_items(raw: Any, allowlist: FrozenSet[str]) -> list:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [_filter_dict(item, allowlist) for item in raw]


def _filter_team_entry(entry: Any) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    rec = entry.get("recommendation")
    return {
        "team_key": entry.get("team_key"),
        "team_summary": _filter_dict(entry.get("team_summary"), TEAM_SUMMARY_ALLOWLIST),
        "roster": _filter_items(entry.get("roster"), ROSTER_ITEM_ALLOWLIST),
        "recommendation": _recommendation_dict(rec) if rec is not None else {},
        "waiver_targets": _filter_items(
            entry.get("waiver_targets"), FREE_AGENT_ALLOWLIST
        ),
    }


def _filter_model_health(raw_mh: Any) -> Dict[str, Any]:
    if isinstance(raw_mh, BacktestReport):
        raw_mh = {
            "window_start": raw_mh.window_start,
            "window_end": raw_mh.window_end,
            "samples_evaluated": raw_mh.samples_evaluated,
            "games_evaluated": raw_mh.games_evaluated,
            "fantasy_point_mae": raw_mh.fantasy_point_mae,
            "totals_brier_score": raw_mh.totals_brier_score,
            "ewma_alpha": raw_mh.recommended_params.ewma_alpha,
            "ridge_l2_penalty": raw_mh.recommended_params.ridge_l2_penalty,
            "poisson_base_rate": raw_mh.recommended_params.poisson_base_rate,
            "suggestions": [str(s) for s in raw_mh.suggestions],
            "improved_over_baseline": bool(raw_mh.improved_over_baseline),
        }
    filtered = _filter_dict(raw_mh, MODEL_HEALTH_ALLOWLIST)
    if "suggestions" in filtered:
        sug = filtered["suggestions"]
        if isinstance(sug, Sequence) and not isinstance(sug, (str, bytes)):
            filtered["suggestions"] = [str(s) for s in sug]
        else:
            filtered["suggestions"] = []
    return filtered


def sanitize_public_report(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Strip every key outside the per-section allowlists."""
    report: Dict[str, Any] = {}
    for key in ("generated_at", "stale_after_hours", "active_slate_date"):
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
            _recommendation_dict(raw["recommendation"]),
            RECOMMENDATION_ALLOWLIST,
        )
    if "games" in raw:
        report["games"] = _filter_items(raw["games"], GAME_ITEM_ALLOWLIST)
    if "free_agents" in raw:
        report["free_agents"] = _filter_items(raw["free_agents"], FREE_AGENT_ALLOWLIST)
    if "teams" in raw:
        report["teams"] = [_filter_team_entry(t) for t in raw["teams"] or []]
    if "point_scorers" in raw:
        report["point_scorers"] = _filter_items(
            raw["point_scorers"], POINT_SCORER_ALLOWLIST
        )
    if "model_health" in raw and raw["model_health"] is not None:
        report["model_health"] = _filter_model_health(raw["model_health"])
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
    for entry in report.get("teams") or []:
        for key in entry:
            if key not in FANTASY_TEAM_ENTRY_ALLOWLIST:
                raise ValueError(f"non-allowlisted teams key: {key!r}")
    for item in report.get("point_scorers") or []:
        for key in item:
            if key not in POINT_SCORER_ALLOWLIST:
                raise ValueError(f"non-allowlisted point_scorers key: {key!r}")
    mh = report.get("model_health")
    if mh is not None:
        if not isinstance(mh, dict):
            raise ValueError("model_health must be a dict")
        for key in mh:
            if key not in MODEL_HEALTH_ALLOWLIST:
                raise ValueError(f"non-allowlisted model_health key: {key!r}")
        if "suggestions" in mh and (
            not isinstance(mh["suggestions"], list)
            or not all(isinstance(s, str) for s in mh["suggestions"])
        ):
            raise ValueError("model_health.suggestions must be a list of strings")
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
    active_slate_date: Optional[str] = None,
    team_summary: Optional[Dict[str, Any]] = None,
    roster: Sequence[Dict[str, Any]] = (),
    recommendation: Optional[Union[SwapEvaluation, Dict[str, Any]]] = None,
    games: Sequence[Dict[str, Any]] = (),
    free_agents: Sequence[Dict[str, Any]] = (),
    teams: Sequence[Dict[str, Any]] = (),
    point_scorers: Sequence[Dict[str, Any]] = (),
    model_health: Optional[Union[BacktestReport, Dict[str, Any]]] = None,
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
    if active_slate_date is not None:
        raw["active_slate_date"] = active_slate_date
    if recommendation is not None:
        raw["recommendation"] = _recommendation_dict(recommendation)
    if teams:
        raw["teams"] = list(teams)
        # Back-compat: top-level sections mirror teams[0] (SRS §6.1).
        first = teams[0]
        raw["team_summary"] = first.get("team_summary") or raw["team_summary"]
        raw["roster"] = first.get("roster") or raw["roster"]
        raw["free_agents"] = first.get("waiver_targets") or raw["free_agents"]
        first_rec = first.get("recommendation")
        if "recommendation" not in raw and first_rec is not None:
            raw["recommendation"] = _recommendation_dict(first_rec)
    if point_scorers:
        raw["point_scorers"] = list(point_scorers)
    if model_health is not None:
        raw["model_health"] = _filter_model_health(model_health)

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
    (8478402, "Connor McDavid", "C", 25, "team1.p.6743"),
    (8480803, "Evan Bouchard", "D", 25, "team1.p.8038"),
    (8475786, "Zach Hyman", "LW", 25, None),
    (8477492, "Nathan MacKinnon", "C", 21, "team2.p.7492"),
    (8478038, "Devon Toews", "D", 21, None),
    (8480065, "Cale Makar", "D", 21, "team2.p.8065"),
    (8479318, "Auston Matthews", "C", 10, "team1.p.9318"),
    (8477939, "William Nylander", "RW", 10, "team1.p.7939"),
    (8478427, "Sebastian Aho", "C", 12, "team2.p.8427"),
    (8480830, "Andrei Svechnikov", "LW", 12, "team2.p.0830"),
    (8482093, "Seth Jarvis", "RW", 12, None),
)
_DEMO_GOALIES: Tuple[Tuple[int, str, int, Optional[str]], ...] = (
    (8479979, "Stuart Skinner", 25, "team1.p.7979"),
    (8480382, "Alexandar Georgiev", 21, "team2.p.0382"),
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

# (player_id) -> (PLAYER_POINTS, PLAYER_GOALS, PLAYER_ASSISTS) American odds.
_DEMO_PROP_ODDS: Dict[int, Tuple[int, int, int]] = {
    8478402: (-300, 120, -200),  # McDavid
    8480803: (-150, 350, -170),  # Bouchard
    8475786: (-170, 150, 140),  # Hyman
    8477492: (-280, 115, -190),  # MacKinnon
    8478038: (130, 420, 110),  # Toews
    8480065: (-190, 200, -160),  # Makar
    8479318: (-260, 110, -150),  # Matthews
    8477939: (130, 190, 140),  # Nylander — soft points price (+EV seed)
    8478427: (-220, 135, -170),  # Aho
    8480830: (-160, 145, -120),  # Svechnikov
    8482093: (160, 300, 220),  # Jarvis — soft prices (+EV seed)
}
_PROP_MARKETS: Tuple[str, str, str] = (
    "PLAYER_POINTS",
    "PLAYER_GOALS",
    "PLAYER_ASSISTS",
)
_TEAM_SLATE_GAME: Dict[int, int] = {
    25: 2026020101,
    21: 2026020101,
    10: 2026020102,
    12: 2026020102,
}


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
    game_id: int,
    total: float,
    over: int,
    under: int,
    captured_at: str,
    sportsbook: str = "PuckLuck Demo",
) -> MarketTotalLine:
    """Construct MarketTotalLine by matching the dataclass's real fields."""
    candidates: Dict[str, Any] = {
        "game_id": game_id,
        "sportsbook": sportsbook,
        "book": sportsbook,
        "book_name": sportsbook,
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

    prop_rows: List[Dict[str, Any]] = []
    for player_id, _name, _pos, team_id, _y in _DEMO_SKATERS:
        point_odds, goal_odds, assist_odds = _DEMO_PROP_ODDS[player_id]
        for market, odds in zip(_PROP_MARKETS, (point_odds, goal_odds, assist_odds)):
            prop_rows.append(
                {
                    "game_id": _TEAM_SLATE_GAME[team_id],
                    "sportsbook": "consensus",
                    "market_type": market,
                    "market": market,
                    "target_id": str(player_id),
                    "line": 0.5,
                    "over_odds": odds,
                    "under_odds": -120 if odds > 0 else 110,
                    "captured_at": "2026-10-22T20:00:00Z",
                    "season": "20262027",
                }
            )
    _insert_fact_rows(conn, "fact_market_lines", prop_rows)

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
    market_col = _pick(columns, "market_type", "market")
    prop_filter = ""
    if market_col:
        marks = ", ".join(f"'{m}'" for m in _PROP_MARKETS)
        prop_filter = f" WHERE {market_col} IS NULL OR {market_col} NOT IN ({marks})"
    sb_col = _pick(columns, "sportsbook", "book")
    rows = [
        dict(zip(columns, r))
        for r in conn.execute(
            f"SELECT * FROM fact_market_lines{prop_filter}"
        ).fetchall()
    ]
    by_game: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        by_game.setdefault(int(row[gid_col]), []).append(row)
    consensus = []
    for gid in sorted(by_game):
        group = by_game[gid]
        real = [
            g
            for g in group
            if sb_col
            and str(g.get(sb_col) or "").lower() not in ("", "consensus", "baseline")
        ]
        pool = real or group
        lines = sorted(float(g[line_col]) for g in pool)
        mid = lines[len(lines) // 2] if len(lines) % 2 else lines[len(lines) // 2 - 1]
        books = {str(g.get(sb_col)) for g in real} if sb_col else set()
        consensus.append(
            {
                "game_id": gid,
                "line": mid,
                "over_odds": max(int(g[over_col]) for g in pool),
                "under_odds": max(int(g[under_col]) for g in pool),
                "sportsbook": f"{len(books)} books" if books else "consensus",
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


# ---------------------------------------------------------------------------
# Phase 7A: live slate rollover, open consensus lines, 2-team fantasy, props
# ---------------------------------------------------------------------------

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
)
ODDSGAP_LINESHOP_URL = "https://theoddsgap.com/api/lineshop"
BOVADA_NHL_URL = (
    "https://www.bovada.lv/services/sports/event/coupon/"
    "events/A/description/hockey/nhl"
)
_ODDSGAP_MATCH_TOLERANCE_SECONDS = 3600
_NHL_FULL_NAMES: Dict[str, str] = {
    "Anaheim Ducks": "ANA",
    "Boston Bruins": "BOS",
    "Buffalo Sabres": "BUF",
    "Calgary Flames": "CGY",
    "Carolina Hurricanes": "CAR",
    "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL",
    "Columbus Blue Jackets": "CBJ",
    "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET",
    "Edmonton Oilers": "EDM",
    "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK",
    "Minnesota Wild": "MIN",
    "Montreal Canadiens": "MTL",
    "Montréal Canadiens": "MTL",
    "Nashville Predators": "NSH",
    "New Jersey Devils": "NJD",
    "New York Islanders": "NYI",
    "New York Rangers": "NYR",
    "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT",
    "San Jose Sharks": "SJS",
    "Seattle Kraken": "SEA",
    "St. Louis Blues": "STL",
    "St Louis Blues": "STL",
    "Tampa Bay Lightning": "TBL",
    "Toronto Maple Leafs": "TOR",
    "Utah Hockey Club": "UTA",
    "Utah Mammoth": "UTA",
    "Vancouver Canucks": "VAN",
    "Vegas Golden Knights": "VGK",
    "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG",
}
_FINISHED_STATES: FrozenSet[str] = frozenset({"FINAL", "OFF"})
_FANTASY_TEAM_PROFILES: Dict[str, Tuple[str, str, str]] = {
    "team1": ("McDavid's Oil", "MO", "High-Stakes H2H"),
    "team2": ("MacKinnon's Avs", "MA", "Keeper Dynasty"),
}


def _game_finished(game: Dict[str, Any]) -> bool:
    return str(game.get("gameState") or "").upper() in _FINISHED_STATES


_LIVE_STATES: FrozenSet[str] = frozenset({"LIVE", "CRIT"})
_POSTPONED_STATES: FrozenSet[str] = frozenset({"PPD", "POSTPONED", "SUSP"})


def _report_status(game_state: str) -> str:
    """NHL ``gameState`` → the report's status vocabulary."""
    state = str(game_state or "").upper()
    if state in _FINISHED_STATES:
        return "Final"
    if state in _LIVE_STATES:
        return "Live"
    if state in _POSTPONED_STATES:
        return "Postponed"
    return "Scheduled"


def _week_days(
    payload: Dict[str, Any], fallback_date: str
) -> Dict[str, List[Dict[str, Any]]]:
    """date -> raw game dicts, from gameWeek blocks or a flat games list."""
    days: Dict[str, List[Dict[str, Any]]] = {}
    week = payload.get("gameWeek")
    if isinstance(week, list):
        for day in week:
            if not isinstance(day, dict) or not day.get("date"):
                continue
            days[str(day["date"])] = [
                g for g in (day.get("games") or []) if isinstance(g, dict)
            ]
    elif isinstance(payload.get("games"), list):
        for g in payload["games"]:
            if not isinstance(g, dict):
                continue
            days.setdefault(str(g.get("gameDate") or fallback_date), []).append(g)
    return days


def resolve_active_slate(
    request_date: str, *, nhl_fetcher: JsonFetcher = default_json_fetcher
) -> Tuple[str, List[ScheduledGame], List[str]]:
    """Pick tonight's slate; roll forward when today's games are done.

    Returns ``(active_slate_date, scheduled_games, completed_dates)`` where
    ``completed_dates`` are finished dates before the active slate (their
    boxscores are ingestable history).
    """
    payload = nhl_fetcher(f"{NHL_API_BASE}/schedule/{request_date}")
    days = _week_days(payload, request_date)

    # The request anchor is a UTC/local date, but an NHL slate outlives it:
    # yesterday's games stay live hours past midnight. Merge the previous day
    # so the slate flips only once its last game is actually finished.
    try:
        prev_day = (date.fromisoformat(request_date) - timedelta(days=1)).isoformat()
    except ValueError:
        prev_day = request_date
    if prev_day != request_date:
        try:
            prev_payload = nhl_fetcher(f"{NHL_API_BASE}/schedule/{prev_day}")
            for day, games in _week_days(prev_payload, prev_day).items():
                days.setdefault(day, games)
        except Exception:
            pass
    earliest = prev_day

    active: Optional[str] = None
    for day in sorted(days):
        if (
            day >= earliest
            and days[day]
            and any(not _game_finished(g) for g in days[day])
        ):
            active = day
            break
    if active is None and payload.get("nextStartDate"):
        next_start = str(payload["nextStartDate"])
        payload2 = nhl_fetcher(f"{NHL_API_BASE}/schedule/{next_start}")
        days2 = _week_days(payload2, next_start)
        for day in sorted(days2):
            if days2[day] and any(not _game_finished(g) for g in days2[day]):
                active = day
                break
        for day, games in days2.items():
            days.setdefault(day, games)
    if active is None:
        played = [d for d in sorted(days) if days[d]]
        active = played[-1] if played else request_date

    completed = sorted(d for d in days if d < active and days[d])
    prev_start = str(payload.get("previousStartDate") or "").strip()
    if len(completed) < 4 and prev_start:
        try:
            prev_payload = nhl_fetcher(f"{NHL_API_BASE}/schedule/{prev_start}")
            for day, games in _week_days(prev_payload, prev_start).items():
                days.setdefault(day, games)
            completed = sorted(d for d in days if d < active and days[d])
        except Exception:
            pass
    scheduled = fetch_schedule(active, fetcher=nhl_fetcher)
    return active, scheduled, completed


def _to_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_open_consensus_lines(
    slate_date: str,
    scheduled_games: Sequence[ScheduledGame],
    *,
    http_fetcher: Callable[[str], Dict[str, Any]] = default_json_fetcher,
    default_line: float = 6.5,
) -> List[MarketTotalLine]:
    """Open-source O/U lines from ESPN's public scoreboard (baseline fallback)."""
    compact = slate_date.replace("-", "")
    try:
        payload = http_fetcher(f"{ESPN_SCOREBOARD_URL}?dates={compact}")
    except Exception:
        payload = {}

    odds_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for event in (payload or {}).get("events") or []:
        if not isinstance(event, dict):
            continue
        for comp in event.get("competitions") or []:
            home_abbr = away_abbr = ""
            for competitor in comp.get("competitors") or []:
                side = str(competitor.get("homeAway") or "").lower()
                abbr = str((competitor.get("team") or {}).get("abbreviation") or "")
                if side == "home":
                    home_abbr = abbr
                elif side == "away":
                    away_abbr = abbr
            for odds in comp.get("odds") or []:
                if home_abbr and away_abbr and isinstance(odds, dict):
                    odds_index.setdefault((home_abbr, away_abbr), odds)

    lines: List[MarketTotalLine] = []
    captured = f"{slate_date}T12:00:00Z"
    for game in scheduled_games:
        entry = odds_index.get((game.home_abbr, game.away_abbr))
        if entry is not None:
            book = str((entry.get("provider") or {}).get("name") or "consensus")
            lines.append(
                _make_market_line(
                    game.game_id,
                    _to_float(entry.get("overUnder"), default_line),
                    _to_int(entry.get("overOdds"), -110),
                    _to_int(entry.get("underOdds"), -110),
                    captured,
                    sportsbook=book,
                )
            )
        else:
            lines.append(
                _make_market_line(
                    game.game_id,
                    default_line,
                    -110,
                    -110,
                    captured,
                    sportsbook="consensus",
                )
            )
    return lines


def _parse_utc(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_oddsgap_total_lines(
    scheduled_games: Sequence[ScheduledGame],
    *,
    http_fetcher: Callable[[str], Dict[str, Any]] = default_json_fetcher,
) -> List[MarketTotalLine]:
    """Per-book totals from theoddsgap.com's public lineshop feed (no key).

    One request returns every scanned game across sports; each NHL game
    carries ``totals_data.all_books`` keyed by book with ``total``,
    ``over_juice`` and ``under_juice``. Games match the NHL schedule on
    team names plus a one-hour commence-time tolerance.
    """
    try:
        payload = http_fetcher(ODDSGAP_LINESHOP_URL)
    except Exception:
        return []
    feed = [
        g
        for g in (payload or {}).get("games") or []
        if str((g or {}).get("sport")) == "icehockey_nhl"
    ]
    if not feed:
        return []
    captured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: List[MarketTotalLine] = []
    for game in scheduled_games:
        start = _parse_utc(game.start_time_utc)
        home_abbr = str(game.home_abbr).upper()
        away_abbr = str(game.away_abbr).upper()
        for entry in feed:
            if _NHL_FULL_NAMES.get(str(entry.get("home") or "")) != home_abbr:
                continue
            if _NHL_FULL_NAMES.get(str(entry.get("away") or "")) != away_abbr:
                continue
            commence = _parse_utc(entry.get("commence_time"))
            if start is not None and commence is not None:
                if (
                    abs((commence - start).total_seconds())
                    > _ODDSGAP_MATCH_TOLERANCE_SECONDS
                ):
                    continue
            all_books = (entry.get("totals_data") or {}).get("all_books") or {}
            for book_key, offer in all_books.items():
                if not isinstance(offer, dict):
                    continue
                try:
                    total = float(offer["total"])
                    over_juice = int(offer["over_juice"])
                    under_juice = int(offer["under_juice"])
                except (KeyError, TypeError, ValueError):
                    continue
                lines.append(
                    _make_market_line(
                        game.game_id,
                        total,
                        over_juice,
                        under_juice,
                        captured,
                        sportsbook=str(offer.get("label") or book_key),
                    )
                )
            break
    return lines


def _norm_name(name: Any) -> str:
    """Accent/punctuation-insensitive player name for book → dim_players joins."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z' ]", "", s.lower())).strip()


def _american(value: Any) -> Optional[int]:
    s = str(value or "").strip().upper()
    if s == "EVEN":
        return 100
    try:
        return int(s.lstrip("+"))
    except ValueError:
        return None


def _bovada_player_name(text: Any) -> Tuple[str, Optional[str]]:
    m = re.match(r"(.+?)\s*\(([A-Za-z]{2,3})\)\s*$", str(text or ""))
    if not m:
        return str(text or "").strip(), None
    return m.group(1).strip(), m.group(2).upper()


def fetch_bovada_props(
    scheduled_games: Sequence[ScheduledGame],
    *,
    http_fetcher: Callable[[str], Any] = default_json_fetcher,
) -> Tuple[List[Dict[str, Any]], List[MarketTotalLine]]:
    """Real prop prices from Bovada's public NHL coupon feed (no key).

    Returns ``(prop_offers, total_lines)`` where prop_offers carry
    ``game_id/team_abbr/name/market/price`` and total_lines are per-game
    ``MarketTotalLine`` rows for the Game Lines total market.
    """
    try:
        payload = http_fetcher(BOVADA_NHL_URL)
    except Exception:
        return [], []
    blocks = payload if isinstance(payload, list) else [payload]
    events = [ev for block in blocks for ev in (block or {}).get("events") or []]
    if not events:
        return [], []

    captured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    offers: List[Dict[str, Any]] = []
    totals: List[MarketTotalLine] = []
    for game in scheduled_games:
        start = _parse_utc(game.start_time_utc)
        home_abbr = str(game.home_abbr).upper()
        away_abbr = str(game.away_abbr).upper()
        for ev in events:
            desc = str(ev.get("description") or "")
            if " @ " not in desc:
                continue
            away_name, home_name = (p.strip() for p in desc.split(" @ ", 1))
            if _NHL_FULL_NAMES.get(home_name) != home_abbr:
                continue
            if _NHL_FULL_NAMES.get(away_name) != away_abbr:
                continue
            start_ms = _to_float(ev.get("startTime"), 0.0)
            if start and start_ms:
                drift = abs(start.timestamp() - start_ms / 1000.0)
                if drift > _ODDSGAP_MATCH_TOLERANCE_SECONDS:
                    continue
            is_live = bool(ev.get("live"))
            for grp in ev.get("displayGroups") or []:
                for mk in grp.get("markets") or []:
                    mdesc = str(mk.get("description") or "")
                    # In-play totals are live-adjusted prices, not the pregame
                    # line — keep them out of the consensus/history.
                    if mdesc == "Total" and not is_live:
                        over, under = {}, {}
                        for oc in mk.get("outcomes") or []:
                            label = str(oc.get("description") or "")
                            price = _american((oc.get("price") or {}).get("american"))
                            hc = _to_float((oc.get("price") or {}).get("handicap"), 0.0)
                            if label == "Over" and price is not None:
                                over = {"line": hc, "odds": price}
                            elif label == "Under" and price is not None:
                                under = {"line": hc, "odds": price}
                        if over and under and over["line"] == under["line"]:
                            totals.append(
                                _make_market_line(
                                    game.game_id,
                                    over["line"],
                                    over["odds"],
                                    under["odds"],
                                    captured,
                                    sportsbook="Bovada",
                                )
                            )
                    elif mdesc.startswith(
                        ("Points Milestones - ", "Assists Milestones - ")
                    ):
                        name, abbr = _bovada_player_name(mdesc.split(" - ", 1)[1])
                        market = (
                            "PLAYER_POINTS"
                            if mdesc.startswith("Points")
                            else "PLAYER_ASSISTS"
                        )
                        want = (
                            "To Score 1+ Points"
                            if market == "PLAYER_POINTS"
                            else "To Record 1+ Assists"
                        )
                        for oc in mk.get("outcomes") or []:
                            if str(oc.get("description")) != want:
                                continue
                            price = _american((oc.get("price") or {}).get("american"))
                            if price is not None:
                                offers.append(
                                    {
                                        "game_id": game.game_id,
                                        "team_abbr": abbr,
                                        "name": name,
                                        "market": market,
                                        "price": price,
                                    }
                                )
                    elif mdesc == "Anytime Goalscorer":
                        for oc in mk.get("outcomes") or []:
                            name, abbr = _bovada_player_name(oc.get("description"))
                            price = _american((oc.get("price") or {}).get("american"))
                            if price is not None:
                                offers.append(
                                    {
                                        "game_id": game.game_id,
                                        "team_abbr": abbr,
                                        "name": name,
                                        "market": "PLAYER_GOALS",
                                        "price": price,
                                    }
                                )
            break
    return offers, totals


def prob_to_fair_american(p: float) -> int:
    """Convert a probability to fair (vig-free) American odds."""
    p = min(max(float(p), 1e-6), 0.999999)
    if p >= 0.5:
        return -int(round(100.0 * p / max(1e-6, 1.0 - p)))
    return int(round(100.0 * (1.0 - p) / max(1e-6, p)))


def _baseline_prop_odds(position: str, projected_fp: float) -> Tuple[int, int, int]:
    """(points, goals, assists) book odds when no consensus prop is stored."""
    elite = projected_fp >= 5.5
    if position == "D":
        return (-140 if elite else 120, 180 if elite else 350, -120 if elite else 160)
    return (-150 if elite else 130, 160 if elite else 260, -130 if elite else 170)


def _player_prop_odds(
    conn: sqlite3.Connection,
    player_id: int,
    slate_game_ids: Optional[FrozenSet[int]] = None,
) -> Dict[str, int]:
    """Stored prop prices for one skater, per-market provenance.

    Rows are scoped to ``slate_game_ids`` (plus ``game_id=0`` baselines) so a
    stale price from an earlier slate can never mask itself as today's line.
    """
    columns = _table_columns(conn, "fact_market_lines")
    market_col = _pick(columns, "market_type", "market")
    target_col = _pick(columns, "target_id")
    over_col = _pick(columns, "over_odds", "over_price")
    if not all((market_col, target_col, over_col)):
        return {}
    sb_col = _pick(columns, "sportsbook", "book")
    gid_col = _pick(columns, "game_id")
    marks = ", ".join(f"'{m}'" for m in _PROP_MARKETS)
    select = f"{market_col} AS m, {over_col} AS o"
    if sb_col:
        select += f", {sb_col} AS sb"
    gid_select = f", {gid_col} AS gid" if gid_col else ""
    rows = conn.execute(
        f"SELECT {select}{gid_select} FROM fact_market_lines "
        f"WHERE {target_col} = ? AND {market_col} IN ({marks})",
        (str(player_id),),
    ).fetchall()
    key_map = {
        "PLAYER_POINTS": "point",
        "PLAYER_GOALS": "goal",
        "PLAYER_ASSISTS": "assist",
    }
    allowed = set(slate_game_ids or ()) | {0}
    out: Dict[str, Any] = {}
    real_keys: set = set()
    for row in rows:
        gid = int(row[3]) if gid_col else 0
        if slate_game_ids is not None and gid not in allowed:
            continue
        sb = str(row[2] or "") if sb_col else ""
        is_real = sb.lower() not in ("", "baseline", "consensus")
        key = key_map.get(str(row[0]).upper())
        if key is None or (not is_real and key in real_keys):
            continue
        if is_real:
            real_keys.add(key)
        out[key] = _to_int(row[1], -110)
        out[f"{key}_src"] = sb or "baseline"
    return out


def compute_point_scorer_projection(
    *,
    player_id: int,
    display_name: str,
    position: str,
    team_abbr: Optional[str],
    opponent_abbr: Optional[str],
    comps: Dict[str, float],
    feats: Dict[str, float],
    projected_fp: float,
    market_odds: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Poisson 1+ point/goal/assist probabilities + prop +EV screening."""
    lam_g = max(0.01, float(comps.get("G", 0.0)))
    lam_a = max(0.01, float(comps.get("A", 0.0)))
    lam_pts = lam_g + lam_a
    prob_point = round(1.0 - math.exp(-lam_pts), 4)
    prob_goal = round(1.0 - math.exp(-lam_g), 4)
    prob_assist = round(1.0 - math.exp(-lam_a), 4)

    stored = market_odds or {}
    base_point, base_goal, base_assist = _baseline_prop_odds(position, projected_fp)
    market_point_odds = int(stored.get("point", base_point))
    market_goal_odds = int(stored.get("goal", base_goal))
    market_assist_odds = int(stored.get("assist", base_assist))

    point_ev = round(compute_ev(prob_point, american_to_decimal(market_point_odds)), 4)
    goal_ev = round(compute_ev(prob_goal, american_to_decimal(market_goal_odds)), 4)
    assist_ev = round(
        compute_ev(prob_assist, american_to_decimal(market_assist_odds)), 4
    )
    # Per-market provenance: each price is real only when its own market row
    # came from a book; a Bovada goalscorer price must not launder baseline
    # points/assists prices into looking real.
    srcs = {
        "point": str(stored.get("point_src") or "baseline"),
        "goal": str(stored.get("goal_src") or "baseline"),
        "assist": str(stored.get("assist_src") or "baseline"),
    }
    candidates = [
        ("POINTS", market_point_odds, point_ev),
        ("GOALS", market_goal_odds, goal_ev),
        ("ASSISTS", market_assist_odds, assist_ev),
    ]
    market_keys = {"POINTS": "point", "GOALS": "goal", "ASSISTS": "assist"}
    real_candidates = [
        candidate
        for candidate in candidates
        if srcs[market_keys[candidate[0]]].lower()
        not in ("", "baseline", "consensus", "demo")
    ]
    best_market, best_odds, best_ev = max(
        real_candidates or candidates, key=lambda candidate: candidate[2]
    )
    best_key = {"POINTS": "point", "GOALS": "goal", "ASSISTS": "assist"}[best_market]
    source = srcs[best_key]
    source_real = source.lower() not in ("", "baseline", "consensus", "demo")
    return {
        "player_id": player_id,
        "display_name": display_name,
        "position": position,
        "team_abbr": team_abbr,
        "opponent_abbr": opponent_abbr,
        "projected_goals": round(lam_g, 2),
        "projected_assists": round(lam_a, 2),
        "projected_points": round(lam_pts, 2),
        "shots_on_goal": round(float(comps.get("SOG", 0.0)), 1),
        "ixg_per60_ewma": round(float(feats.get("ixg_per60_ewma", 0.0)), 3),
        "pp_toi_share_ewma": round(float(feats.get("pp_toi_share_ewma", 0.0)), 3),
        "prob_point": prob_point,
        "prob_goal": prob_goal,
        "prob_assist": prob_assist,
        "fair_point_odds": prob_to_fair_american(prob_point),
        "fair_goal_odds": prob_to_fair_american(prob_goal),
        "fair_assist_odds": prob_to_fair_american(prob_assist),
        "market_point_odds": market_point_odds,
        "market_goal_odds": market_goal_odds,
        "market_assist_odds": market_assist_odds,
        "market_point_src": srcs["point"],
        "market_goal_src": srcs["goal"],
        "market_assist_src": srcs["assist"],
        "point_ev": point_ev,
        "goal_ev": goal_ev,
        "assist_ev": assist_ev,
        "best_prop_market": best_market,
        "best_prop_odds": int(best_odds),
        "best_prop_ev": round(float(best_ev), 4),
        # Only count a prop as a pick when priced by a real book —
        # baseline-estimated prices can never produce a pick.
        "is_positive_ev": bool(best_ev >= 0.035 and source_real),
        "odds_source": source,
    }


def _seed_local_fantasy_teams(conn: sqlite3.Connection) -> None:
    """Tag 2 local fantasy teams (4 skaters + 1 goalie each) sans Yahoo OAuth."""
    player_cols = _table_columns(conn, "dim_players")
    pos_col = _pick(player_cols, "primary_position", "position")
    name_col = _pick(player_cols, "display_name", "name")
    rows = conn.execute(
        f"SELECT player_id, {name_col}, {pos_col} FROM dim_players "
        "WHERE yahoo_player_id IS NULL "
        "ORDER BY (player_id IN ("
        "  SELECT DISTINCT player_id FROM fact_skater_games"
        "  UNION SELECT DISTINCT goalie_id FROM fact_goalie_games"
        ")) DESC, player_id"
    ).fetchall()
    skaters = [r for r in rows if str(r[2]) != "G"]
    goalies = [r for r in rows if str(r[2]) == "G"]
    tagged: List[Tuple[Any, str]] = []
    for idx, row in enumerate(skaters[:8]):
        tagged.append((row, f"team{idx // 4 + 1}.p.{int(row[0])}"))
    for idx, row in enumerate(goalies[:2]):
        tagged.append((row, f"team{idx + 1}.p.{int(row[0])}"))
    for row, yahoo_id in tagged:
        db.upsert_player(
            conn,
            int(row[0]),
            str(row[1]),
            str(row[2]),
            yahoo_player_id=yahoo_id,
        )


def _seed_baseline_props(conn: sqlite3.Connection, slate_date: str) -> None:
    """Baseline consensus prop rows so +EV is evaluated for every skater."""
    player_cols = _table_columns(conn, "dim_players")
    pos_col = _pick(player_cols, "primary_position", "position")
    market_columns = set(_table_columns(conn, "fact_market_lines"))
    if not (
        _pick(list(market_columns), "market_type", "market")
        and "target_id" in market_columns
    ):
        return
    captured = f"{slate_date}T12:00:00Z"
    skaters = conn.execute(
        f"SELECT player_id, {pos_col} FROM dim_players WHERE {pos_col} != 'G'"
    ).fetchall()
    rows: List[Dict[str, Any]] = []
    for player_id, position in skaters:
        base = _baseline_prop_odds(str(position), 4.0)
        for market, odds in zip(_PROP_MARKETS, base):
            rows.append(
                {
                    "game_id": 0,
                    "sportsbook": "baseline",
                    "market_type": market,
                    "market": market,
                    "target_id": str(int(player_id)),
                    "line": 0.5,
                    "over_odds": odds,
                    "under_odds": -120 if odds > 0 else 110,
                    "captured_at": captured,
                    "season": "20262027",
                }
            )
    _insert_fact_rows(conn, "fact_market_lines", rows)


def _seed_bovada_props(
    conn: sqlite3.Connection,
    scheduled: Sequence[ScheduledGame],
    *,
    http_fetcher: Callable[[str], Any] = default_json_fetcher,
) -> None:
    """Persist real Bovada prop prices matched to dim_players by name+team."""
    offers, totals = fetch_bovada_props(scheduled, http_fetcher=http_fetcher)
    for line in totals:
        upsert_total_line(conn, line)
    # Drop Bovada total rows for games already started — they were captured
    # while live and are not pregame closes.
    live_ids = [
        int(g.game_id) for g in scheduled if _report_status(g.game_state) != "Scheduled"
    ]
    if live_ids:
        marks = ", ".join("?" for _ in live_ids)
        conn.execute(
            "DELETE FROM fact_market_lines WHERE sportsbook = 'Bovada' "
            f"AND market_type = 'TOTAL_GOALS' AND game_id IN ({marks})",
            live_ids,
        )
    if not offers:
        return
    abbrs = _team_abbrs(conn)
    name_col = _pick(_table_columns(conn, "dim_players"), "display_name", "name")
    if not name_col:
        return
    # dim_players has no team_id — join each player's most recent skater game.
    lookup: Dict[Tuple[str, str], int] = {}
    for pid, nm, tid in conn.execute(
        f"SELECT p.player_id, p.{name_col}, s.team_id FROM dim_players p "
        "JOIN fact_skater_games s ON s.player_id = p.player_id "
        "AND s.game_date = (SELECT MAX(f2.game_date) FROM fact_skater_games f2 "
        "WHERE f2.player_id = p.player_id)"
    ).fetchall():
        ab = abbrs.get(int(tid)) if tid is not None else None
        if ab:
            lookup.setdefault((_norm_name(nm), ab.upper()), int(pid))
    captured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: List[Dict[str, Any]] = []
    for offer in offers:
        pid = lookup.get((_norm_name(offer["name"]), offer["team_abbr"]))
        if pid is None:
            continue
        rows.append(
            {
                "game_id": int(offer["game_id"]),
                "sportsbook": "Bovada",
                "market_type": offer["market"],
                "market": offer["market"],
                "target_id": str(pid),
                "line": 0.5,
                "over_odds": int(offer["price"]),
                "under_odds": -120 if int(offer["price"]) > 0 else 110,
                "captured_at": captured,
                "season": "20262027",
            }
        )
    _insert_fact_rows(conn, "fact_market_lines", rows)


def extract_moneypuck_metrics(
    skaters_df: Any = None, goalies_df: Any = None
) -> Dict[int, Dict[str, float]]:
    """Build per-player per-game MoneyPuck advanced metric lookup for boxscores."""
    metrics: Dict[int, Dict[str, float]] = {}
    if skaters_df is not None and len(skaters_df) > 0:
        df = skaters_df
        if "situation" in df.columns:
            all_rows = df[df["situation"] == "all"]
            if len(all_rows) > 0:
                df = all_rows
        for row in df.to_dict("records"):
            pid = _to_int(row.get("playerId"), 0)
            if pid <= 0:
                continue
            gp = max(1.0, _to_float(row.get("games_played"), 1.0))
            ixg = _to_float(row.get("I_F_xGoals"), 0.0) / gp
            icorsi = int(round(_to_float(row.get("I_F_shotAttempts"), 0.0) / gp))
            ihdcf = int(round(_to_float(row.get("I_F_highDangerShots"), 0.0) / gp))
            xgf = _to_float(row.get("OnIce_F_xGoals"), 0.0)
            xga = _to_float(row.get("OnIce_A_xGoals"), 0.0)
            xg_share = xgf / (xgf + xga) if (xgf + xga) > 0 else 0.5
            metrics[pid] = {
                "ixg": round(ixg, 4),
                "icorsi": icorsi,
                "ihdcf": ihdcf,
                "on_ice_xg_share": round(xg_share, 4),
            }
    if goalies_df is not None and len(goalies_df) > 0:
        df = goalies_df
        if "situation" in df.columns:
            all_rows = df[df["situation"] == "all"]
            if len(all_rows) > 0:
                df = all_rows
        for row in df.to_dict("records"):
            pid = _to_int(row.get("playerId"), 0)
            if pid <= 0:
                continue
            gp = max(1.0, _to_float(row.get("games_played"), 1.0))
            gsax = _to_float(row.get("goalsSavedAboveExpected"), 0.0) / gp
            entry = dict(metrics.get(pid) or {})
            entry["gsax"] = round(gsax, 4)
            metrics[pid] = entry
    return metrics


def sync_live_nhl_slate(
    conn: sqlite3.Connection,
    target_date: str = "2026-10-23",
    *,
    nhl_fetcher: JsonFetcher = default_json_fetcher,
    odds_fetcher: Optional[Callable[[str], Dict[str, Any]]] = None,
    lookback_days: int = 3,
    seed_local_teams: bool = True,
) -> Tuple[str, List[ScheduledGame]]:
    """Sync tonight's real slate: rollover, history, rosters, lines, teams.

    Returns ``(active_slate_date, scheduled_games)``.
    """
    demo_row = conn.execute(
        "SELECT 1 FROM dim_teams WHERE team_id = 25 AND abbreviation = 'EDM'"
    ).fetchone()
    if demo_row is not None:
        conn.execute("DELETE FROM fact_skater_games")
        conn.execute("DELETE FROM fact_goalie_games")
        conn.execute("DELETE FROM fact_market_lines")
        conn.execute("DELETE FROM dim_players")
        conn.execute("DELETE FROM dim_teams")

    active, scheduled, completed = resolve_active_slate(
        target_date, nhl_fetcher=nhl_fetcher
    )

    adv_metrics: Dict[int, Dict[str, float]] = {}
    try:
        update_cache()
        adv_metrics = extract_moneypuck_metrics(load_skaters(), load_goalies())
    except Exception:
        adv_metrics = {}

    for day in completed[-max(1, int(lookback_days)) :]:
        try:
            day_games = fetch_schedule(day, fetcher=nhl_fetcher)
        except Exception:
            continue
        for game in day_games:
            try:
                ingest_boxscore(
                    conn,
                    game.game_id,
                    fetcher=nhl_fetcher,
                    advanced_metrics=adv_metrics,
                )
            except Exception:
                continue

    for game in scheduled:
        db.upsert_team(
            conn,
            game.home_team_id,
            game.home_abbr,
            game.home_name,
            game.venue,
            altitude_ft=TEAM_ALTITUDE_FT.get(game.home_abbr, 0),
        )
        existing_away = conn.execute(
            "SELECT venue FROM dim_teams WHERE team_id = ?",
            (game.away_team_id,),
        ).fetchone()
        away_venue = (
            existing_away[0] if existing_away and existing_away[0] else game.away_name
        )
        db.upsert_team(
            conn,
            game.away_team_id,
            game.away_abbr,
            game.away_name,
            away_venue,
            altitude_ft=TEAM_ALTITUDE_FT.get(game.away_abbr, 0),
        )
        for abbr in (game.home_abbr, game.away_abbr):
            try:
                ingest_team_roster(conn, abbr, fetcher=nhl_fetcher, commit=False)
            except Exception:
                continue

    # Market lines are keyed (game_id, sportsbook, market_type): each book's
    # latest line is kept per game and prior slates stay for backtesting.
    # Non-slate rows never reach the report — _build_game_items filters by
    # slate_matchups.
    for line in fetch_open_consensus_lines(
        active, scheduled, http_fetcher=odds_fetcher or nhl_fetcher
    ):
        upsert_total_line(conn, line)
    for line in fetch_oddsgap_total_lines(
        scheduled, http_fetcher=odds_fetcher or nhl_fetcher
    ):
        upsert_total_line(conn, line)

    if seed_local_teams and not os.environ.get("YAHOO_CLIENT_ID"):
        _seed_local_fantasy_teams(conn)
    _seed_baseline_props(conn, active)
    _seed_bovada_props(conn, scheduled, http_fetcher=odds_fetcher or nhl_fetcher)
    conn.commit()
    return active, scheduled


def run_warehouse_pipeline(
    conn: sqlite3.Connection,
    *,
    as_of_date: str = "2026-10-23",
    stat_weights: Optional[Dict[str, float]] = None,
    team_summary: Optional[Dict[str, Any]] = None,
    horizon_weeks: float = 2.5,
    generated_at: Optional[str] = None,
    active_slate_date: Optional[str] = None,
    slate_matchups: Optional[Dict[int, Tuple[int, int]]] = None,
    slate_start_times: Optional[Dict[int, str]] = None,
    slate_states: Optional[Dict[int, Tuple[str, Optional[int], Optional[int]]]] = None,
    model_params: Optional[TunedModelParams] = None,
    model_params_path: Union[str, Path] = DEFAULT_MODEL_PARAMS_PATH,
    model_health: Optional[Union[BacktestReport, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run FeatureBuilder -> Ridge -> Rescorer -> Solver/Poisson -> report."""
    weights = stat_weights or DEFAULT_STAT_WEIGHTS
    rescorer = ScoringRescorer(weights)
    params = model_params or load_model_params(model_params_path)

    fb = FeatureBuilder(conn, alpha=params.ewma_alpha)
    X, y_by_comp, _sample_keys = fb.build_training_dataset(
        as_of_date, min_prior_games=1
    )
    model = (
        ComponentRidgeModel(l2_penalty=params.ridge_l2_penalty).fit(X, y_by_comp)
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
    # Historical rows supply team identity, never tonight's opponent.
    slate_by_team = {}
    for gid, (home_id, away_id) in (slate_matchups or {}).items():
        slate_by_team[home_id] = (gid, away_id, True)
        slate_by_team[away_id] = (gid, home_id, False)

    fa_items: List[Dict[str, Any]] = []
    fa_candidates: List[CandidatePlayer] = []
    skater_records: List[Dict[str, Any]] = []
    goalie_records: List[Dict[str, Any]] = []

    for player_id, display_name, position, yahoo_id in players:
        player_id = int(player_id)
        yahoo_prefix = str(yahoo_id).split(".", 1)[0] if yahoo_id is not None else None
        if position == "G":
            if player_id not in latest_goalie:
                continue
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
            goalie_records.append(
                {
                    "yahoo_prefix": yahoo_prefix,
                    "item": {
                        "player_id": player_id,
                        "display_name": display_name,
                        "position": "G",
                        "selected_slot": "G",
                        "team_abbr": abbrs.get(team_id),
                        "opponent_abbr": abbrs.get(opponent_id),
                        "goalie_status": "CONFIRMED",
                        "projected_fp": proj,
                        "shots_on_goal": 0,
                    },
                }
            )
            continue
        if player_id not in latest_skater:
            continue
        team_id, opponent_id = latest_skater[player_id]
        is_home = True
        if team_id in slate_by_team:
            _, opponent_id, is_home = slate_by_team[team_id]
        fv = fb.build_player_features(
            player_id,
            team_id,
            opponent_id,
            as_of_date,
            is_home=is_home,
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
        candidate = _make_candidate(
            player_id,
            display_name,
            proj_fp,
            (proj_fp,),
            ros_weekly,
            droppable=yahoo_id is not None,
        )

        record = {
            "player_id": player_id,
            "display_name": display_name,
            "position": position,
            "team_id": team_id,
            "opponent_id": opponent_id,
            "yahoo_prefix": yahoo_prefix,
            "comps": comps,
            "feats": feats,
            "proj_fp": proj_fp,
            "candidate": candidate,
        }
        if yahoo_id is not None:
            record["item"] = {
                "player_id": player_id,
                "display_name": display_name,
                "position": position,
                "selected_slot": position,
                "team_abbr": abbrs.get(team_id),
                "opponent_abbr": abbrs.get(opponent_id),
                "opp_def_rtg_rel": round(float(feats.get("opp_def_rtg_rel", 0.0)), 3),
                "projected_fp": proj_fp,
                "shots_on_goal": round(comps.get("SOG", 0.0), 1),
                "ixg": comps.get("iXG", 0.0),
                "goals": comps.get("G", 0.0),
                "assists": comps.get("A", 0.0),
                "blocks": comps.get("BLK", 0.0),
                "hits": comps.get("HIT", 0.0),
            }
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
            fa_candidates.append(candidate)
        skater_records.append(record)

    fa_items.sort(key=lambda f: -float(f["ixg_per60_ewma"]))
    game_items, slate_team_ids = _build_game_items(
        conn,
        as_of_date,
        abbrs,
        slate_matchups,
        slate_start_times=slate_start_times,
        slate_states=slate_states,
        base_rate=params.poisson_base_rate,
    )

    # Two fantasy teams: group rostered players by yahoo prefix; if all share
    # one prefix (e.g. "449"), split evenly into two squads.
    rostered = [r for r in skater_records if r["yahoo_prefix"] is not None]
    prefixes = sorted({r["yahoo_prefix"] for r in rostered})
    if len(prefixes) >= 2:
        groups = [
            (p, [r for r in rostered if r["yahoo_prefix"] == p]) for p in prefixes[:2]
        ]
    else:
        half = (len(rostered) + 1) // 2
        groups = [("team1", rostered[:half]), ("team2", rostered[half:])]
    while len(groups) < 2:
        groups.append((f"team{len(groups) + 1}", []))

    # Goalies join the team matching their yahoo prefix; with a single
    # prefix (split-evenly mode) they split one-per-team in order.
    rostered_goalies = [g for g in goalie_records if g["yahoo_prefix"] is not None]
    if len(prefixes) >= 2:
        goalie_groups = [
            [g["item"] for g in rostered_goalies if g["yahoo_prefix"] == groups[i][0]]
            for i in range(2)
        ]
    else:
        goalie_items = [g["item"] for g in rostered_goalies]
        goalie_groups = [goalie_items[:1], goalie_items[1:2]]

    team_entries: List[Dict[str, Any]] = []
    for idx, (prefix, members) in enumerate(groups[:2]):
        team_key = prefix if prefix.startswith("team") else f"team_{idx + 1}"
        name, initials, league = _FANTASY_TEAM_PROFILES.get(
            team_key,
            _FANTASY_TEAM_PROFILES.get(
                prefix, (f"PuckLuck Team {idx + 1}", f"T{idx + 1}", "Demo League")
            ),
        )
        team_roster = sorted(
            (r["item"] for r in members),
            key=lambda item: -float(item["projected_fp"]),
        )
        team_roster.extend(goalie_groups[idx])
        team_candidates = [r["candidate"] for r in members]
        recommendation = solve_best_stream(
            fa_candidates, team_candidates, horizon_weeks=horizon_weeks
        )
        if recommendation is None and fa_candidates and team_candidates:
            worst = min(team_candidates, key=lambda c: c.fp_ros_weekly_avg)
            best = max(fa_candidates, key=lambda c: c.fp_tonight)
            recommendation = evaluate_swap(best, worst, horizon_weeks=horizon_weeks)
        summary = dict(
            team_summary
            or {
                "team_name": name,
                "team_initials": initials,
                "league_name": league,
                "adds_used": 0,
                "adds_limit": 4,
                "matchup_score": "0 - 0",
            }
        )
        summary["team_name"] = name
        summary["team_initials"] = initials
        summary["league_name"] = league
        team_entries.append(
            {
                "team_key": team_key,
                "team_summary": summary,
                "roster": team_roster,
                "recommendation": recommendation,
                "waiver_targets": fa_items,
            }
        )

    # Point/goal/assist scorers on the active slate.
    scorer_records = [
        r
        for r in skater_records
        if (
            r["team_id"] in slate_by_team
            if slate_matchups is not None
            else not slate_team_ids or r["team_id"] in slate_team_ids
        )
    ]
    ended_team_ids = set()
    if slate_states and slate_matchups:
        for gid, (st, _h, _a) in slate_states.items():
            if _report_status(st) != "Scheduled":
                ended_team_ids.update(slate_matchups.get(gid, ()))

    def _scorer(r):
        item = compute_point_scorer_projection(
            player_id=r["player_id"],
            display_name=r["display_name"],
            position=r["position"],
            team_abbr=abbrs.get(r["team_id"]),
            opponent_abbr=abbrs.get(r["opponent_id"]),
            comps=r["comps"],
            feats=r["feats"],
            projected_fp=r["proj_fp"],
            market_odds=_player_prop_odds(
                conn,
                r["player_id"],
                (
                    frozenset({slate_by_team[r["team_id"]][0]})
                    if slate_matchups is not None
                    else None
                ),
            ),
        )
        # A prop is only bettable before the player's game starts.
        if r["team_id"] in ended_team_ids:
            item["is_positive_ev"] = False
        return item

    point_scorers = [_scorer(r) for r in scorer_records]
    point_scorers.sort(key=lambda s: (-s["prob_point"], -s["best_prop_ev"]))

    return build_public_report(
        generated_at=generated_at,
        active_slate_date=active_slate_date or as_of_date,
        games=game_items,
        teams=team_entries,
        point_scorers=point_scorers,
        model_health=model_health,
    )


def _build_game_items(
    conn: sqlite3.Connection,
    as_of_date: str,
    abbrs: Dict[int, str],
    slate_matchups: Optional[Dict[int, Tuple[int, int]]] = None,
    *,
    slate_start_times: Optional[Dict[int, str]] = None,
    slate_states: Optional[Dict[int, Tuple[str, Optional[int], Optional[int]]]] = None,
    base_rate: float = 3.05,
) -> Tuple[List[Dict[str, Any]], FrozenSet[int]]:
    """Build the games list; returns (items, team_ids on tonight's slate)."""
    consensus = _market_game_rows(conn)
    if not consensus:
        return [], frozenset()
    matchup_map = dict(_DEMO_MATCHUPS)
    matchup_map.update(slate_matchups or {})
    fallback_pairs = _recent_pair_stream(conn, as_of_date)
    items: List[Dict[str, Any]] = []
    slate_team_ids: set = set()
    eg_params = inspect.signature(compute_expected_goals).parameters
    accepts_base_rate = "base_rate" in eg_params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in eg_params.values()
    )
    for idx, market in enumerate(consensus):
        game_id = market["game_id"]
        if slate_matchups is not None and game_id not in slate_matchups:
            continue
        pairing = matchup_map.get(game_id)
        if pairing is None and fallback_pairs:
            pairing = fallback_pairs[idx % len(fallback_pairs)]
        if pairing is None:
            continue
        home_id, away_id = pairing
        slate_team_ids.update((home_id, away_id))

        home_inputs, away_inputs = build_matchup_inputs_from_db(
            conn, home_id, away_id, as_of_date
        )
        expected = (
            compute_expected_goals(home_inputs, away_inputs, base_rate=base_rate)
            if accepts_base_rate
            else compute_expected_goals(home_inputs, away_inputs)
        )
        lam_home, lam_away = _lambdas(expected)
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
        state, final_home, final_away = (slate_states or {}).get(
            game_id, ("", None, None)
        )
        status = _report_status(state)
        item = {
            "game_id": game_id,
            "start_time_utc": (slate_start_times or {}).get(game_id)
            or f"{as_of_date}T01:00:00Z",
            "home_abbr": abbrs.get(home_id),
            "away_abbr": abbrs.get(away_id),
            "status": status,
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
            # Only a not-yet-started game can carry a bettable pick.
            "is_positive_ev": bool(best_ev >= 0.035 and status == "Scheduled"),
            "line_source": str(market.get("sportsbook") or "consensus"),
            "home_goalie_status": "CONFIRMED",
            "away_goalie_status": "PROJECTED",
        }
        if status == "Final" and final_home is not None and final_away is not None:
            total = final_home + final_away
            item["final_home_score"] = final_home
            item["final_away_score"] = final_away
            item["final_total"] = total
            if total != float(market["line"]):
                over_hit = total > float(market["line"])
                item["lean_hit"] = bool(over_hit) == (best_side == "OVER")
        items.append(item)
    return items, frozenset(slate_team_ids)


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
    parser.add_argument(
        "--live",
        action="store_true",
        help="Sync tonight's real NHL slate (schedule, boxscores, rosters, "
        "open consensus lines) before running the pipeline.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=7,
        help="Completed slate days to ingest boxscores for (live mode).",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run walk-forward backtest, self-tune model_params.json, and "
        "include model_health in the report.",
    )
    parser.add_argument(
        "--params-path",
        default=os.environ.get(
            "PUCKLUCK_MODEL_PARAMS_PATH", str(DEFAULT_MODEL_PARAMS_PATH)
        ),
    )
    args = parser.parse_args(argv)

    conn = db.init_db(args.db_path)
    try:
        if args.live:
            live_date = os.environ.get("PUCKLUCK_LIVE_DATE") or args.date
            active_date, scheduled = sync_live_nhl_slate(
                conn, live_date, lookback_days=args.lookback_days
            )
            slate_matchups = {
                g.game_id: (g.home_team_id, g.away_team_id) for g in scheduled
            }
            slate_start_times = {g.game_id: g.start_time_utc for g in scheduled}
            slate_states = {
                g.game_id: (g.game_state, g.home_score, g.away_score) for g in scheduled
            }
        else:
            active_date = args.date
            slate_matchups = None
            slate_start_times = None
            slate_states = None
            if args.demo or _dim_players_empty(conn):
                seed_demo_warehouse(conn, as_of_date=args.date)
        bt_report: Optional[BacktestReport] = None
        if args.backtest:
            bt_report = run_walk_forward_backtest(conn, as_of_date=active_date)
            save_model_params(bt_report.recommended_params, args.params_path)
        report = run_warehouse_pipeline(
            conn,
            as_of_date=active_date,
            active_slate_date=active_date,
            slate_matchups=slate_matchups,
            slate_start_times=slate_start_times,
            slate_states=slate_states,
            model_params_path=args.params_path,
            model_health=bt_report,
        )
        path = write_report_json(report, args.output)
    finally:
        conn.close()
    print(f"wrote {path}")
    return path


if __name__ == "__main__":
    main()
