"""Walk-forward backtesting & hyperparameter self-tuner (Phase 8 / SRS §8).

Evaluates component Ridge regressions and Bivariate Poisson game totals
strictly out-of-sample across a rolling walk-forward window
``[as_of_date - window_days, as_of_date)``. For every evaluation date
``d`` in the window, models are fit exclusively on warehouse rows with
``game_date < d`` before predicting games on ``d``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from betting.poisson import (
    DEFAULT_BASE_RATE,
    build_matchup_inputs_from_db,
    compute_expected_goals,
    price_total_goals,
)
from features.builder import (
    COMPONENT_COLUMNS,
    COMPONENTS,
    EWMA_ALPHA,
    FeatureBuilder,
)
from models.ridge import ComponentRidgeModel, ScoringRescorer

DEFAULT_MODEL_PARAMS_PATH = Path("data/model_params.json")
CANDIDATE_EWMA_ALPHAS: Tuple[float, ...] = (0.05, 0.08, 0.12, 0.16)
CANDIDATE_RIDGE_LAMBDAS: Tuple[float, ...] = (0.25, 0.5, 1.0, 2.5, 5.0)
CANDIDATE_BASE_RATES: Tuple[float, ...] = (2.90, 3.00, 3.05, 3.10, 3.20)

DEFAULT_STAT_WEIGHTS: Dict[str, float] = {
    "G": 3.0,
    "A": 2.0,
    "SOG": 0.4,
    "HIT": 0.5,
    "BLK": 0.5,
}
_PROP_MARKETS: Tuple[str, str, str] = (
    "PLAYER_POINTS",
    "PLAYER_GOALS",
    "PLAYER_ASSISTS",
)


@dataclass(frozen=True)
class TunedModelParams:
    ewma_alpha: float = 0.08
    ridge_l2_penalty: float = 1.0
    poisson_base_rate: float = 3.05
    updated_at: str = ""


@dataclass(frozen=True)
class BacktestReport:
    window_start: str
    window_end: str
    samples_evaluated: int
    games_evaluated: int
    component_mae: Dict[str, float]
    fantasy_point_mae: float
    totals_brier_score: float
    recommended_params: TunedModelParams
    suggestions: Tuple[str, ...]
    improved_over_baseline: bool


def load_model_params(
    path: Union[str, Path] = DEFAULT_MODEL_PARAMS_PATH,
) -> TunedModelParams:
    """Load persisted model hyperparameters or fall back to baseline defaults."""
    target = Path(path)
    if not target.is_file():
        return TunedModelParams()
    try:
        raw = json.loads(target.read_text())
    except (OSError, ValueError, TypeError):
        return TunedModelParams()
    if not isinstance(raw, dict):
        return TunedModelParams()
    return TunedModelParams(
        ewma_alpha=float(raw.get("ewma_alpha", EWMA_ALPHA)),
        ridge_l2_penalty=float(raw.get("ridge_l2_penalty", 1.0)),
        poisson_base_rate=float(raw.get("poisson_base_rate", DEFAULT_BASE_RATE)),
        updated_at=str(raw.get("updated_at", "")),
    )


def save_model_params(
    params: TunedModelParams,
    path: Union[str, Path] = DEFAULT_MODEL_PARAMS_PATH,
) -> Path:
    """Atomically persist tuned model hyperparameters via .tmp + os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(asdict(params), indent=2, sort_keys=False) + "\n")
    os.replace(tmp, target)
    return target


def _game_total_lines(conn: sqlite3.Connection) -> Dict[int, float]:
    """Lookup per-game total line from fact_market_lines when available."""
    cols = [str(r[1]) for r in conn.execute("PRAGMA table_info(fact_market_lines)")]
    if "game_id" not in cols or "line" not in cols:
        return {}
    market_col = (
        "market_type"
        if "market_type" in cols
        else ("market" if "market" in cols else None)
    )
    where_clause = ""
    if market_col:
        marks = ", ".join(f"'{m}'" for m in _PROP_MARKETS)
        where_clause = f" WHERE {market_col} IS NULL OR {market_col} NOT IN ({marks})"
    rows = conn.execute(
        f"SELECT game_id, line FROM fact_market_lines{where_clause}"
    ).fetchall()
    by_game: Dict[int, List[float]] = {}
    for gid, line in rows:
        if gid is not None and line is not None:
            by_game.setdefault(int(gid), []).append(float(line))
    out: Dict[int, float] = {}
    for gid, vals in by_game.items():
        vals_sorted = sorted(vals)
        mid = len(vals_sorted) // 2
        out[gid] = vals_sorted[mid] if len(vals_sorted) % 2 else vals_sorted[mid - 1]
    return out


def _precompute_skater_samples(
    conn: sqlite3.Connection,
    as_of_date: str,
    alpha: float,
) -> List[Tuple[str, np.ndarray, Dict[str, float]]]:
    """Precompute (game_date, feature_array, actual_components) for games < as_of_date.

    Each sample's feature vector is built as of its own ``game_date`` so it
    only ever sees strictly prior rows (``< game_date``).
    """
    fb = FeatureBuilder(conn, alpha=alpha)
    rows = conn.execute(
        "SELECT game_id, player_id, team_id, opponent_id, is_home, "
        "rest_days, game_date, goals, assists, shots_on_goal, blocks, "
        "hits, ixg "
        "FROM fact_skater_games WHERE game_date < ? "
        "ORDER BY game_date ASC, game_id ASC, player_id ASC",
        (as_of_date,),
    ).fetchall()

    samples: List[Tuple[str, np.ndarray, Dict[str, float]]] = []
    for r in rows:
        gdate = str(r["game_date"])
        fv = fb.build_player_features(
            int(r["player_id"]),
            int(r["team_id"]),
            int(r["opponent_id"]),
            gdate,
            is_home=r["is_home"],
            team_rest=r["rest_days"],
        )
        if fv.games_sampled < 1:
            continue
        actual = {comp: float(r[col]) for comp, col in COMPONENT_COLUMNS.items()}
        samples.append((gdate, fv.to_array(), actual))
    return samples


def _evaluate_skater_grid(
    conn: sqlite3.Connection,
    as_of_date: str,
    eval_dates: Tuple[str, ...],
    rescorer: ScoringRescorer,
) -> Tuple[
    float,
    float,
    int,
    Dict[str, float],
    float,
    float,
]:
    """Grid-search (alpha, l2_penalty) across walk-forward evaluation dates."""
    eval_date_set = set(eval_dates)
    samples_by_alpha: Dict[float, List[Tuple[str, np.ndarray, Dict[str, float]]]] = {
        alpha: _precompute_skater_samples(conn, as_of_date, alpha)
        for alpha in CANDIDATE_EWMA_ALPHAS
    }

    results: Dict[Tuple[float, float], Tuple[int, Dict[str, float], float]] = {}
    for alpha in CANDIDATE_EWMA_ALPHAS:
        all_samples = samples_by_alpha[alpha]
        for l2_penalty in CANDIDATE_RIDGE_LAMBDAS:
            comp_errors: Dict[str, List[float]] = {c: [] for c in COMPONENTS}
            fp_errors: List[float] = []

            for d in eval_dates:
                train = [s for s in all_samples if s[0] < d]
                test = [s for s in all_samples if s[0] == d and d in eval_date_set]
                if not train or not test:
                    continue
                X_train = np.vstack([s[1] for s in train])
                y_train = {
                    c: np.asarray([s[2][c] for s in train], dtype=np.float64)
                    for c in COMPONENTS
                }
                model = ComponentRidgeModel(l2_penalty=l2_penalty).fit(X_train, y_train)
                X_test = np.vstack([s[1] for s in test])
                preds_batch = model.predict_batch(X_test)

                for idx, (_gdate, _x, actual) in enumerate(test):
                    pred_comps = {c: float(preds_batch[c][idx]) for c in COMPONENTS}
                    for c in COMPONENTS:
                        comp_errors[c].append(abs(pred_comps[c] - actual[c]))
                    pred_fp = rescorer.score(pred_comps)
                    actual_fp = rescorer.score(actual)
                    fp_errors.append(abs(pred_fp - actual_fp))

            n_eval = len(fp_errors)
            comp_mae = {
                c: round(float(np.mean(comp_errors[c])), 4) if comp_errors[c] else 0.0
                for c in COMPONENTS
            }
            fp_mae = round(float(np.mean(fp_errors)), 4) if fp_errors else 0.0
            results[(alpha, l2_penalty)] = (n_eval, comp_mae, fp_mae)

    baseline_key = (EWMA_ALPHA, 1.0)
    base_n, base_comp_mae, base_fp_mae = results.get(
        baseline_key, (0, {c: 0.0 for c in COMPONENTS}, 0.0)
    )
    best_alpha, best_l2 = baseline_key
    best_n, best_comp_mae, best_fp_mae = base_n, base_comp_mae, base_fp_mae

    for (alpha, l2_penalty), (n_eval, comp_mae, fp_mae) in results.items():
        if n_eval > 0 and (best_n == 0 or fp_mae < best_fp_mae - 1e-9):
            best_alpha, best_l2 = alpha, l2_penalty
            best_n, best_comp_mae, best_fp_mae = n_eval, comp_mae, fp_mae

    return (
        best_alpha,
        best_l2,
        best_n,
        best_comp_mae,
        best_fp_mae,
        base_fp_mae,
    )


def _evaluate_poisson_grid(
    conn: sqlite3.Connection,
    eval_dates: Tuple[str, ...],
) -> Tuple[float, int, float, float]:
    """Grid-search Poisson base_rate across walk-forward evaluation dates."""
    if not eval_dates:
        return DEFAULT_BASE_RATE, 0, 0.0, 0.0

    lines_by_game = _game_total_lines(conn)
    eval_games: List[Tuple[str, int, int, int, float, float]] = []

    for d in eval_dates:
        prior_row = conn.execute(
            "SELECT COUNT(*) FROM fact_skater_games WHERE game_date < ?",
            (d,),
        ).fetchone()
        if not prior_row or int(prior_row[0]) == 0:
            continue

        game_rows = conn.execute(
            "SELECT game_id, "
            "MAX(CASE WHEN is_home = 1 THEN team_id ELSE opponent_id END) AS home_id, "
            "MAX(CASE WHEN is_home = 1 THEN opponent_id ELSE team_id END) AS away_id, "
            "SUM(goals) AS total_goals "
            "FROM fact_skater_games WHERE game_date = ? "
            "GROUP BY game_id ORDER BY game_id ASC",
            (d,),
        ).fetchall()
        for r in game_rows:
            gid = int(r["game_id"])
            home_id = int(r["home_id"])
            away_id = int(r["away_id"])
            total_goals = float(r["total_goals"] or 0.0)
            line = float(lines_by_game.get(gid, 6.5))
            if total_goals > line:
                outcome = 1.0
            elif total_goals < line:
                outcome = 0.0
            else:
                outcome = 0.5
            eval_games.append((d, gid, home_id, away_id, line, outcome))

    if not eval_games:
        return DEFAULT_BASE_RATE, 0, 0.0, 0.0

    matchup_cache: Dict[Tuple[str, int, int], Tuple[object, object]] = {}
    for d, _gid, home_id, away_id, _line, _outcome in eval_games:
        key = (d, home_id, away_id)
        if key not in matchup_cache:
            matchup_cache[key] = build_matchup_inputs_from_db(conn, home_id, away_id, d)

    brier_by_rate: Dict[float, float] = {}
    for base_rate in CANDIDATE_BASE_RATES:
        sq_errors: List[float] = []
        for d, _gid, home_id, away_id, line, outcome in eval_games:
            home_inp, away_inp = matchup_cache[(d, home_id, away_id)]
            lam_h, lam_a = compute_expected_goals(
                home_inp, away_inp, base_rate=base_rate
            )
            price = price_total_goals(lam_h, lam_a, line)
            sq_errors.append((float(price.prob_over) - outcome) ** 2)
        brier_by_rate[base_rate] = round(float(np.mean(sq_errors)), 4)

    base_brier = brier_by_rate.get(DEFAULT_BASE_RATE, 0.0)
    best_rate = DEFAULT_BASE_RATE
    best_brier = base_brier
    for base_rate, brier in brier_by_rate.items():
        if brier < best_brier - 1e-9:
            best_rate = base_rate
            best_brier = brier

    return best_rate, len(eval_games), best_brier, base_brier


def run_walk_forward_backtest(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    window_days: int = 14,
    stat_weights: Optional[Dict[str, float]] = None,
) -> BacktestReport:
    """Evaluate walk-forward out-of-sample accuracy and tune hyperparameters."""
    end_dt = date.fromisoformat(as_of_date)
    start_dt = end_dt - timedelta(days=max(1, int(window_days)))
    window_start = start_dt.isoformat()
    window_end = as_of_date

    date_rows = conn.execute(
        "SELECT DISTINCT game_date FROM fact_skater_games "
        "WHERE game_date >= ? AND game_date < ? "
        "ORDER BY game_date ASC",
        (window_start, window_end),
    ).fetchall()
    eval_dates = tuple(str(r[0]) for r in date_rows)

    weights = stat_weights or DEFAULT_STAT_WEIGHTS
    rescorer = ScoringRescorer(weights)

    (
        best_alpha,
        best_l2,
        samples_evaluated,
        component_mae,
        best_fp_mae,
        base_fp_mae,
    ) = _evaluate_skater_grid(conn, as_of_date, eval_dates, rescorer)

    (
        best_base_rate,
        games_evaluated,
        best_brier,
        base_brier,
    ) = _evaluate_poisson_grid(conn, eval_dates)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    recommended = TunedModelParams(
        ewma_alpha=best_alpha,
        ridge_l2_penalty=best_l2,
        poisson_base_rate=best_base_rate,
        updated_at=now_iso,
    )

    suggestions: List[str] = []
    improved_fp = (best_alpha, best_l2) != (EWMA_ALPHA, 1.0) and (
        best_fp_mae < base_fp_mae - 1e-9
    )
    improved_brier = (best_base_rate != DEFAULT_BASE_RATE) and (
        best_brier < base_brier - 1e-9
    )

    if improved_fp:
        pct = (
            (base_fp_mae - best_fp_mae) / base_fp_mae * 100.0
            if base_fp_mae > 0
            else 0.0
        )
        suggestions.append(
            f"Update EWMA alpha -> {best_alpha:.2f}, Ridge L2 -> {best_l2:g} "
            f"(reduces out-of-sample FP MAE by {pct:.1f}%)"
        )
    if improved_brier:
        brier_pct = (
            (base_brier - best_brier) / base_brier * 100.0 if base_brier > 0 else 0.0
        )
        suggestions.append(
            f"Update Poisson base rate -> {best_base_rate:.2f} "
            f"(reduces totals Brier score by {brier_pct:.1f}%)"
        )

    if not suggestions:
        if samples_evaluated == 0 and games_evaluated == 0:
            suggestions.append(
                "Insufficient walk-forward samples in window; retaining baseline parameters."
            )
        else:
            suggestions.append(
                f"Retain baseline parameters (optimal across current {int(window_days)}-day walk-forward window)"
            )

    return BacktestReport(
        window_start=window_start,
        window_end=window_end,
        samples_evaluated=samples_evaluated,
        games_evaluated=games_evaluated,
        component_mae=component_mae,
        fantasy_point_mae=best_fp_mae,
        totals_brier_score=best_brier,
        recommended_params=recommended,
        suggestions=tuple(suggestions),
        improved_over_baseline=bool(improved_fp or improved_brier),
    )
