"""Feature pipeline — 15-game EWMA process metrics (SRS §4.1).

Builds the 10-dimensional feature vector X_{p,g} for player ``p`` in a
target game ``g`` exclusively from warehouse rows with
``game_date < as_of_date`` (SRS §8 temporal integrity: nothing dated on
or after ``as_of_date`` can ever enter a feature vector or training
matrix).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

EWMA_ALPHA: float = 0.08
DEFAULT_REST_DAYS: int = 2

FEATURE_NAMES: Tuple[str, ...] = (
    "ixg_per60_ewma",
    "icorsi_per60_ewma",
    "ihdcf_per60_ewma",
    "pp_toi_share_ewma",
    "toi_minutes_ewma",
    "opp_def_rtg_rel",
    "opp_goalie_gsax_per60",
    "rest_diff",
    "b2b_flag",
    "home_flag",
)

COMPONENT_COLUMNS: Dict[str, str] = {
    "SOG": "shots_on_goal",
    "iXG": "ixg",
    "G": "goals",
    "A": "assists",
    "BLK": "blocks",
    "HIT": "hits",
}

COMPONENTS: Tuple[str, ...] = ("SOG", "iXG", "G", "A", "BLK", "HIT")


def compute_ewma(values: Sequence[float], alpha: float = EWMA_ALPHA) -> float:
    """Exponentially weighted moving average, oldest -> newest."""
    if not values:
        return 0.0
    m = float(values[0])
    for v in values[1:]:
        m = alpha * float(v) + (1.0 - alpha) * m
    return float(m)


@dataclass(frozen=True)
class PlayerFeatureVector:
    player_id: int
    as_of_date: str
    games_sampled: int
    features: Dict[str, float]  # keys match FEATURE_NAMES

    def to_array(self) -> np.ndarray:
        """Return 1D float64 numpy array ordered by FEATURE_NAMES."""
        return np.array([self.features[k] for k in FEATURE_NAMES], dtype=np.float64)


def team_rest_days(
    conn: sqlite3.Connection,
    team_id: int,
    as_of_date: str,
    default_rest: int = DEFAULT_REST_DAYS,
) -> int:
    """Days since the team's last game strictly before ``as_of_date``."""
    row = conn.execute(
        "SELECT MAX(game_date) AS prev FROM fact_skater_games "
        "WHERE team_id = ? AND game_date < ?",
        (team_id, as_of_date),
    ).fetchone()
    prev = row["prev"] if row else None
    if not prev:
        return default_rest
    gap = (date.fromisoformat(as_of_date) - date.fromisoformat(prev)).days
    return max(0, gap - 1)


class FeatureBuilder:
    """Builds per-player feature vectors and component training matrices."""

    def __init__(self, conn: sqlite3.Connection, *, alpha: float = EWMA_ALPHA) -> None:
        self._conn = conn
        self._alpha = float(alpha)
        # Per-as_of_date caches for the two league-wide aggregates.
        self._pp_denom_cache: Dict[str, Dict[Tuple[int, int], float]] = {}
        self._xga_cache: Dict[str, Tuple[Dict[int, List[float]], float]] = {}

    # -- cached aggregates ---------------------------------------------------

    def _pp_denominators(self, as_of_date: str) -> Dict[Tuple[int, int], float]:
        """Per (game_id, team_id) PP-share denominator: max(max_pp, sum_pp/5)."""
        if as_of_date in self._pp_denom_cache:
            return self._pp_denom_cache[as_of_date]
        rows = self._conn.execute(
            "SELECT game_id, team_id, pp_toi_seconds FROM fact_skater_games "
            "WHERE game_date < ?",
            (as_of_date,),
        ).fetchall()
        groups: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for r in rows:
            groups[(r["game_id"], r["team_id"])].append(r["pp_toi_seconds"])
        denoms = {key: max(max(vals), sum(vals) / 5.0) for key, vals in groups.items()}
        self._pp_denom_cache[as_of_date] = denoms
        return denoms

    def _xga_by_opponent(self, as_of_date: str) -> Tuple[Dict[int, List[float]], float]:
        """Per-game xGA totals grouped by defending team, plus league mean."""
        if as_of_date in self._xga_cache:
            return self._xga_cache[as_of_date]
        rows = self._conn.execute(
            "SELECT game_id, opponent_id, SUM(ixg) AS xga FROM fact_skater_games "
            "WHERE game_date < ? GROUP BY game_id, opponent_id",
            (as_of_date,),
        ).fetchall()
        by_opp: Dict[int, List[float]] = defaultdict(list)
        all_xga: List[float] = []
        for r in rows:
            xga = float(r["xga"])
            by_opp[int(r["opponent_id"])].append(xga)
            all_xga.append(xga)
        league_mean = float(np.mean(all_xga)) if all_xga else 0.0
        result = (dict(by_opp), league_mean)
        self._xga_cache[as_of_date] = result
        return result

    # -- individual features --------------------------------------------------

    def _opponent_def_rtg_rel(self, opponent_id: int, as_of_date: str) -> float:
        by_opp, league_mean = self._xga_by_opponent(as_of_date)
        opp_games = by_opp.get(opponent_id)
        if opp_games and league_mean > 0:
            return float(np.mean(opp_games)) / league_mean - 1.0
        return 0.0

    def _goalie_gsax_per60(self, goalie_id: int, as_of_date: str) -> float:
        rows = self._conn.execute(
            "SELECT gsax, toi_seconds FROM fact_goalie_games "
            "WHERE goalie_id = ? AND game_date < ? AND toi_seconds > 0 "
            "ORDER BY game_date ASC, game_id ASC",
            (goalie_id, as_of_date),
        ).fetchall()
        series = [r["gsax"] * 3600.0 / r["toi_seconds"] for r in rows]
        return compute_ewma(series, alpha=self._alpha)

    def _opponent_goalie_gsax_per60(
        self, opponent_id: int, opponent_goalie_id: Optional[int], as_of_date: str
    ) -> float:
        goalie_id = opponent_goalie_id
        if goalie_id is None:
            row = self._conn.execute(
                "SELECT goalie_id FROM fact_goalie_games "
                "WHERE team_id = ? AND game_date < ? "
                "ORDER BY game_date DESC, game_id DESC LIMIT 1",
                (opponent_id, as_of_date),
            ).fetchone()
            goalie_id = row["goalie_id"] if row else None
        if goalie_id is None:
            return 0.0
        return self._goalie_gsax_per60(goalie_id, as_of_date)

    # -- public API -------------------------------------------------------------

    def build_player_features(
        self,
        player_id: int,
        team_id: int,
        opponent_id: int,
        as_of_date: str,
        *,
        is_home: Union[bool, int],
        opponent_goalie_id: Optional[int] = None,
        team_rest: Optional[int] = None,
        opp_rest: Optional[int] = None,
    ) -> PlayerFeatureVector:
        """10-dim feature vector for player p's game on/after as_of_date.

        Only warehouse rows with ``game_date < as_of_date`` are read.
        """
        rows = self._conn.execute(
            "SELECT game_id, team_id, toi_seconds, pp_toi_seconds, ixg, icorsi, ihdcf "
            "FROM fact_skater_games "
            "WHERE player_id = ? AND game_date < ? "
            "ORDER BY game_date ASC, game_id ASC",
            (player_id, as_of_date),
        ).fetchall()

        denoms = self._pp_denominators(as_of_date)
        ixg60: List[float] = []
        icorsi60: List[float] = []
        ihdcf60: List[float] = []
        pp_share: List[float] = []
        toi_min: List[float] = []
        for r in rows:
            toi = int(r["toi_seconds"])
            if toi > 0:
                scale = 3600.0 / toi
                ixg60.append(float(r["ixg"]) * scale)
                icorsi60.append(float(r["icorsi"]) * scale)
                ihdcf60.append(float(r["ihdcf"]) * scale)
                toi_min.append(toi / 60.0)
            else:
                ixg60.append(0.0)
                icorsi60.append(0.0)
                ihdcf60.append(0.0)
                toi_min.append(0.0)
            denom = denoms.get((r["game_id"], r["team_id"]), 0.0)
            pp_share.append(min(1.0, r["pp_toi_seconds"] / denom) if denom > 0 else 0.0)

        t_rest = (
            team_rest_days(self._conn, team_id, as_of_date)
            if team_rest is None
            else int(team_rest)
        )
        o_rest = (
            team_rest_days(self._conn, opponent_id, as_of_date)
            if opp_rest is None
            else int(opp_rest)
        )

        features = {
            "ixg_per60_ewma": compute_ewma(ixg60, self._alpha),
            "icorsi_per60_ewma": compute_ewma(icorsi60, self._alpha),
            "ihdcf_per60_ewma": compute_ewma(ihdcf60, self._alpha),
            "pp_toi_share_ewma": compute_ewma(pp_share, self._alpha),
            "toi_minutes_ewma": compute_ewma(toi_min, self._alpha),
            "opp_def_rtg_rel": self._opponent_def_rtg_rel(opponent_id, as_of_date),
            "opp_goalie_gsax_per60": self._opponent_goalie_gsax_per60(
                opponent_id, opponent_goalie_id, as_of_date
            ),
            "rest_diff": float(t_rest - o_rest),
            "b2b_flag": 1.0 if t_rest == 0 else 0.0,
            "home_flag": 1.0 if is_home else 0.0,
        }
        return PlayerFeatureVector(
            player_id=player_id,
            as_of_date=as_of_date,
            games_sampled=len(rows),
            features=features,
        )

    def build_training_dataset(
        self,
        as_of_date: str,
        *,
        min_prior_games: int = 1,
    ) -> Tuple[np.ndarray, Dict[str, np.ndarray], List[Tuple[int, int]]]:
        """Build (X, y_by_component, sample_keys) from games < as_of_date.

        Each historical row's features are computed as of that row's own
        ``game_date`` so no training sample can see its own (or future)
        stats. Rows with fewer than ``min_prior_games`` prior appearances
        are skipped.
        """
        rows = self._conn.execute(
            "SELECT game_id, player_id, team_id, opponent_id, is_home, "
            "rest_days, game_date, goals, assists, shots_on_goal, blocks, "
            "hits, ixg "
            "FROM fact_skater_games WHERE game_date < ? "
            "ORDER BY game_date ASC, game_id ASC",
            (as_of_date,),
        ).fetchall()

        x_rows: List[np.ndarray] = []
        y_cols: Dict[str, List[float]] = {c: [] for c in COMPONENTS}
        sample_keys: List[Tuple[int, int]] = []
        for r in rows:
            fv = self.build_player_features(
                r["player_id"],
                r["team_id"],
                r["opponent_id"],
                r["game_date"],
                is_home=r["is_home"],
                team_rest=r["rest_days"],
            )
            if fv.games_sampled < min_prior_games:
                continue
            x_rows.append(fv.to_array())
            for comp, col in COMPONENT_COLUMNS.items():
                y_cols[comp].append(float(r[col]))
            sample_keys.append((int(r["game_id"]), int(r["player_id"])))

        X = (
            np.vstack(x_rows)
            if x_rows
            else np.empty((0, len(FEATURE_NAMES)), dtype=np.float64)
        )
        y_by_component = {
            c: np.asarray(vals, dtype=np.float64) for c, vals in y_cols.items()
        }
        return X, y_by_component, sample_keys
