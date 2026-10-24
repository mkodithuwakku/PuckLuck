"""Daily refresh entrypoint + allowlisted ``dist/report.json`` export.

The static report is pushed to GitHub Pages — a PUBLIC surface — so the
export enforces a strict key allowlist and hard-rejects anything that
looks like a secret or a local filesystem path (SRS §6.2/§7.1). A report
that fails validation is never written.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Sequence, Tuple, Union

from fantasy.solver import SwapEvaluation

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


def main() -> None:
    """Daily launchd entrypoint: emit the allowlisted public report."""
    report = build_public_report()
    path = write_report_json(report)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
