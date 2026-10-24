"""Bivariate Poisson game-total model (SRS §5.1/§5.2).

Each team's goals follow an independent Poisson process whose intensity
lambda multiplies league base rate by relative offense, opposing
defense, venue, rest, and the opposing goalie's rolling GSAx/60
adjustment. The joint PMF is evaluated over a [0, 15] x [0, 15] grid to
price Over/Under/Push at a given total line.

All warehouse access in ``build_matchup_inputs_from_db`` filters
``game_date < as_of_date`` (SRS §8 temporal integrity).
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from features.builder import EWMA_ALPHA, compute_ewma, team_rest_days

DEFAULT_BASE_RATE: float = 3.05
DEFAULT_VENUE_HOME: float = 1.04
UNCONFIRMED_GOALIE_PENALTY: float = 0.22
MIN_LAMBDA: float = 0.05
MAX_GOALS_GRID: int = 15


@dataclass(frozen=True)
class TeamMatchupInputs:
    team_offense: float = 1.0  # relative to league avg (1.0 = avg)
    opponent_defense: float = 1.0  # opposing team xGA rel. to league avg
    rest_days: int = 2
    opposing_goalie_gsax_per60: float = 0.0
    opposing_goalie_confirmed: bool = True


@dataclass(frozen=True)
class TotalLinePrice:
    line: float
    prob_over: float
    prob_under: float
    prob_push: float
    lambda_home: float
    lambda_away: float

    @property
    def expected_total(self) -> float:
        return float(self.lambda_home + self.lambda_away)


def goalie_adjustment(gsax_per60: float) -> float:
    """``1 - GSAx/60 / 3.0`` clamped to [0.5, 1.5] (SRS §5.1)."""
    return min(1.5, max(0.5, 1.0 - float(gsax_per60) / 3.0))


def rest_factor(rest_days: int) -> float:
    """B2B fatigue discount / rested boost on team lambda."""
    if rest_days <= 0:
        return 0.95
    if rest_days == 1:
        return 1.00
    return 1.02


def compute_expected_goals(
    home: TeamMatchupInputs,
    away: TeamMatchupInputs,
    *,
    base_rate: float = DEFAULT_BASE_RATE,
    venue_home: float = DEFAULT_VENUE_HOME,
) -> Tuple[float, float]:
    """Return (lambda_home, lambda_away) per SRS §5.1."""
    lambda_home = (
        base_rate
        * home.team_offense
        * away.opponent_defense
        * venue_home
        * rest_factor(home.rest_days)
        * goalie_adjustment(home.opposing_goalie_gsax_per60)
    )
    lambda_away = (
        base_rate
        * away.team_offense
        * home.opponent_defense
        * rest_factor(away.rest_days)
        * goalie_adjustment(away.opposing_goalie_gsax_per60)
    )
    if not home.opposing_goalie_confirmed:  # away goalie unconfirmed
        lambda_home += UNCONFIRMED_GOALIE_PENALTY
    if not away.opposing_goalie_confirmed:  # home goalie unconfirmed
        lambda_away += UNCONFIRMED_GOALIE_PENALTY
    return max(MIN_LAMBDA, lambda_home), max(MIN_LAMBDA, lambda_away)


def _poisson_pmf(lam: float, max_goals: int) -> np.ndarray:
    if lam <= 0:
        out = np.zeros(max_goals + 1)
        out[0] = 1.0
        return out
    ks = np.arange(max_goals + 1, dtype=np.float64)
    log_pmf = np.array([-lam + k * math.log(lam) - math.lgamma(k + 1.0) for k in ks])
    return np.exp(log_pmf)


def poisson_pmf_grid(
    lambda_home: float,
    lambda_away: float,
    *,
    max_goals: int = MAX_GOALS_GRID,
) -> np.ndarray:
    """Joint (max_goals+1)^2 matrix P[h, a] = P(H=h) * P(A=a)."""
    pmf_h = _poisson_pmf(float(lambda_home), max_goals)
    pmf_a = _poisson_pmf(float(lambda_away), max_goals)
    return np.outer(pmf_h, pmf_a)


def price_total_goals(
    lambda_home: float,
    lambda_away: float,
    line: float,
    *,
    max_goals: int = MAX_GOALS_GRID,
) -> TotalLinePrice:
    """P(Over/Under/Push L) from the bivariate Poisson grid (SRS §5.2)."""
    grid = poisson_pmf_grid(lambda_home, lambda_away, max_goals=max_goals)
    totals = np.add.outer(np.arange(max_goals + 1), np.arange(max_goals + 1))
    prob_over = float(grid[totals > line].sum())
    prob_under = float(grid[totals < line].sum())
    prob_push = float(grid[totals == line].sum())
    return TotalLinePrice(
        line=float(line),
        prob_over=prob_over,
        prob_under=prob_under,
        prob_push=prob_push,
        lambda_home=float(lambda_home),
        lambda_away=float(lambda_away),
    )


def _team_scoring_rates(
    conn: sqlite3.Connection, as_of_date: str
) -> Tuple[Dict[int, float], Dict[int, float], float]:
    """Per-team offense/defense factors strictly before ``as_of_date``.

    Returns (offense_factor, defense_factor, league_mean_per_game_xg)
    where offense[team] = mean per-game SUM(ixg) generated, and
    defense[team] = mean per-game SUM(ixg) allowed (as opponent_id).
    """
    rows = conn.execute(
        "SELECT game_id, team_id, opponent_id, SUM(ixg) AS xg "
        "FROM fact_skater_games WHERE game_date < ? "
        "GROUP BY game_id, team_id, opponent_id",
        (as_of_date,),
    ).fetchall()
    generated: Dict[int, List[float]] = defaultdict(list)
    allowed: Dict[int, List[float]] = defaultdict(list)
    totals: List[float] = []
    for r in rows:
        xg = float(r["xg"])
        generated[int(r["team_id"])].append(xg)
        allowed[int(r["opponent_id"])].append(xg)
        totals.append(xg)
    league_mean = float(np.mean(totals)) if totals else 0.0

    offense: Dict[int, float] = {}
    defense: Dict[int, float] = {}
    if league_mean > 0:
        for tid, vals in generated.items():
            offense[tid] = float(np.mean(vals)) / league_mean
        for tid, vals in allowed.items():
            defense[tid] = float(np.mean(vals)) / league_mean
    return offense, defense, league_mean


def _team_goalie_gsax_per60(
    conn: sqlite3.Connection,
    team_id: int,
    goalie_id: Optional[int],
    as_of_date: str,
) -> float:
    """Rolling EWMA of the goalie's GSAx/60 strictly before as_of_date."""
    if goalie_id is None:
        row = conn.execute(
            "SELECT goalie_id FROM fact_goalie_games "
            "WHERE team_id = ? AND game_date < ? "
            "ORDER BY game_date DESC, game_id DESC LIMIT 1",
            (team_id, as_of_date),
        ).fetchone()
        goalie_id = row["goalie_id"] if row else None
    if goalie_id is None:
        return 0.0
    rows = conn.execute(
        "SELECT gsax, toi_seconds FROM fact_goalie_games "
        "WHERE goalie_id = ? AND game_date < ? AND toi_seconds > 0 "
        "ORDER BY game_date ASC, game_id ASC",
        (goalie_id, as_of_date),
    ).fetchall()
    series = [r["gsax"] * 3600.0 / r["toi_seconds"] for r in rows]
    return compute_ewma(series, alpha=EWMA_ALPHA)


def build_matchup_inputs_from_db(
    conn: sqlite3.Connection,
    home_team_id: int,
    away_team_id: int,
    as_of_date: str,
    *,
    home_goalie_id: Optional[int] = None,
    away_goalie_id: Optional[int] = None,
    home_goalie_confirmed: bool = True,
    away_goalie_confirmed: bool = True,
) -> Tuple[TeamMatchupInputs, TeamMatchupInputs]:
    """Derive (home_inputs, away_inputs) from rows < as_of_date only."""
    offense, defense, _ = _team_scoring_rates(conn, as_of_date)

    home = TeamMatchupInputs(
        team_offense=offense.get(home_team_id, 1.0),
        opponent_defense=defense.get(away_team_id, 1.0),
        rest_days=team_rest_days(conn, home_team_id, as_of_date),
        opposing_goalie_gsax_per60=_team_goalie_gsax_per60(
            conn, away_team_id, away_goalie_id, as_of_date
        ),
        opposing_goalie_confirmed=away_goalie_confirmed,
    )
    away = TeamMatchupInputs(
        team_offense=offense.get(away_team_id, 1.0),
        opponent_defense=defense.get(home_team_id, 1.0),
        rest_days=team_rest_days(conn, away_team_id, as_of_date),
        opposing_goalie_gsax_per60=_team_goalie_gsax_per60(
            conn, home_team_id, home_goalie_id, as_of_date
        ),
        opposing_goalie_confirmed=home_goalie_confirmed,
    )
    return home, away
