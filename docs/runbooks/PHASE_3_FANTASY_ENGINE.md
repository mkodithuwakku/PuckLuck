# Runbook: Phase 3 — Statistical Modeling, Feature Pipeline & H-Week Add/Drop Solver

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Implement `features/builder.py`, `models/ridge.py`, `fantasy/solver.py` (plus package `__init__.py` files) and their deterministic offline test suites `tests/test_features.py`, `tests/test_ridge.py`, and `tests/test_solver.py`.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_3_FANTASY_ENGINE.md` — This specification.
2. `db.py` — SQLite schema (`fact_skater_games`, `fact_goalie_games`, `dim_players`, `dim_teams`) and row helpers.
3. `conftest.py` — Offline socket blocker.

### Files to Create / Modify
- `features/__init__.py` (CREATE)
- `features/builder.py` (CREATE)
- `models/__init__.py` (CREATE)
- `models/ridge.py` (CREATE)
- `fantasy/__init__.py` (CREATE)
- `fantasy/solver.py` (CREATE)
- `tests/test_features.py` (CREATE)
- `tests/test_ridge.py` (CREATE)
- `tests/test_solver.py` (CREATE)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `tests/test_db.py`, `tests/test_moneypuck.py`, `tests/test_crosswalk.py`, `tests/test_yahoo.py`, `tests/test_nhl.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Module 1 Specification: `features/builder.py` (SRS §4.1, §8)

### 2.1 Constants & Ordering Contract
- `EWMA_ALPHA: float = 0.08` ($\approx 15$-game horizon per SRS §4.1)
- `DEFAULT_REST_DAYS: int = 2`
- `FEATURE_NAMES: Tuple[str, ...] = (`
  1. `"ixg_per60_ewma"`,
  2. `"icorsi_per60_ewma"`,
  3. `"ihdcf_per60_ewma"`,
  4. `"pp_toi_share_ewma"`,
  5. `"toi_minutes_ewma"`,
  6. `"opp_def_rtg_rel"`,
  7. `"opp_goalie_gsax_per60"`,
  8. `"rest_diff"`,
  9. `"b2b_flag"`,
  10. `"home_flag"`,
  `)`
- `COMPONENT_COLUMNS: Dict[str, str] = {`
  - `"SOG": "shots_on_goal"`,
  - `"iXG": "ixg"`,
  - `"G": "goals"`,
  - `"A": "assists"`,
  - `"BLK": "blocks"`,
  - `"HIT": "hits"`,
  `}`
- `COMPONENTS: Tuple[str, ...] = ("SOG", "iXG", "G", "A", "BLK", "HIT")`

### 2.2 Mathematical Definitions
1. **EWMA Function (`compute_ewma(values: Sequence[float], alpha: float = EWMA_ALPHA) -> float`):**
   - Given a chronological sequence $v_0, v_1, \dots, v_{k-1}$ (oldest to newest):
     - If `values` is empty, return `0.0`.
     - Initialize $m = float(v_0)$. For each $v$ in $v_1 \dots v_{k-1}$: $m = \alpha \cdot float(v) + (1.0 - \alpha) \cdot m$.
     - Return $float(m)$.
2. **Per-Game Skater Process Rates (for each historical game $g$ with `game_date < as_of_date`, ordered by `game_date ASC, game_id ASC`):**
   - If `toi_seconds > 0`:
     - $\text{ixg}_{60} = \text{ixg} \times \frac{3600.0}{\text{toi\_seconds}}$
     - $\text{icorsi}_{60} = \text{icorsi} \times \frac{3600.0}{\text{toi\_seconds}}$
     - $\text{ihdcf}_{60} = \text{ihdcf} \times \frac{3600.0}{\text{toi\_seconds}}$
     - $\text{toi\_minutes} = \frac{\text{toi\_seconds}}{60.0}$
   - If `toi_seconds <= 0`, all four rates for that game are `0.0`.
   - **PP TOI Share (`pp_toi_share`):**
     - For a given `(game_id, team_id)`, define team PP ice-time denominator from `fact_skater_games`:
       $$\text{denom} = \max\left(\max_{p \in (g, \text{team})} \text{pp\_toi\_seconds}_p,\ \frac{1}{5.0}\sum_{p \in (g, \text{team})} \text{pp\_toi\_seconds}_p\right)$$
     - If $\text{denom} > 0$, $\text{pp\_share} = \min\left(1.0, \frac{\text{pp\_toi\_seconds}}{\text{denom}}\right)$; else $0.0$.
3. **Opponent Relative Defensive Rating (`opp_def_rtg_rel`):**
   - Strictly from `fact_skater_games` where `game_date < as_of_date`:
   - Group by `(game_id, opponent_id)` to obtain total xG allowed (`SUM(ixg)`) by each defending team `opponent_id` in each game.
   - Let $\overline{\text{xGA}}_{\text{opp}}$ be the mean per-game xGA for `opponent_id` across its games strictly before `as_of_date`, and $\overline{\text{xGA}}_{\text{league}}$ be the mean per-game xGA across all `(game_id, opponent_id)` pairs strictly before `as_of_date`.
   - If `opponent_id` has at least one prior game and $\overline{\text{xGA}}_{\text{league}} > 0$:
     $$\text{DefRtg}_{\text{rel}} = \frac{\overline{\text{xGA}}_{\text{opp}}}{\overline{\text{xGA}}_{\text{league}}} - 1.0$$
     Else return `0.0`.
4. **Opponent Starting Goalie Rolling GSAx/60 (`opp_goalie_gsax_per60`):**
   - If `opponent_goalie_id` is `None`, look up the most recent goalie for `opponent_id` in `fact_goalie_games` with `game_date < as_of_date` (`ORDER BY game_date DESC, game_id DESC LIMIT 1`). If still `None`, return `0.0`.
   - Fetch all rows from `fact_goalie_games` for `goalie_id = opponent_goalie_id` with `game_date < as_of_date` and `toi_seconds > 0`, ordered by `game_date ASC, game_id ASC`.
   - For each game, compute $\text{gsax}_{60} = \text{gsax} \times \frac{3600.0}{\text{toi\_seconds}}$, then return `compute_ewma(gsax_60_series, alpha=alpha)`.
5. **Schedule Context (`rest_diff`, `b2b_flag`, `home_flag`):**
   - Helper `team_rest_days(conn, team_id, as_of_date, default_rest=DEFAULT_REST_DAYS) -> int`: queries `SELECT MAX(game_date) FROM fact_skater_games WHERE team_id = ? AND game_date < ?`. Returns `default_rest` if `None`, else `max(0, (date.fromisoformat(as_of_date) - date.fromisoformat(prev)).days - 1)`.
   - `rest_diff = float(team_rest - opp_rest)`
   - `b2b_flag = 1.0 if team_rest == 0 else 0.0`
   - `home_flag = 1.0 if is_home else 0.0`

### 2.3 Data Contracts & `FeatureBuilder` Class
```python
@dataclass(frozen=True)
class PlayerFeatureVector:
    player_id: int
    as_of_date: str
    games_sampled: int
    features: Dict[str, float]  # keys match FEATURE_NAMES

    def to_array(self) -> np.ndarray:
        """Return 1D float64 numpy array ordered by FEATURE_NAMES."""
        return np.array([self.features[k] for k in FEATURE_NAMES], dtype=np.float64)


class FeatureBuilder:
    def __init__(self, conn: sqlite3.Connection, *, alpha: float = EWMA_ALPHA) -> None: ...

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
    ) -> PlayerFeatureVector: ...

    def build_training_dataset(
        self,
        as_of_date: str,
        *,
        min_prior_games: int = 1,
    ) -> Tuple[np.ndarray, Dict[str, np.ndarray], List[Tuple[int, int]]]:
        """Build (X, y_by_component, sample_keys) from games strictly before as_of_date.

        For each historical skater game row r with r.game_date < as_of_date,
        computes build_player_features(..., as_of_date=r.game_date, team_rest=r.rest_days).
        Skips rows where games_sampled < min_prior_games.
        Returns:
          - X: shape (n_samples, len(FEATURE_NAMES)) float64
          - y_by_component: dict mapping each c in COMPONENTS to 1D float64 array of shape (n_samples,)
          - sample_keys: list of (game_id, player_id) tuples in chronological order
        """
```

---

## 3. Module 2 Specification: `models/ridge.py` (SRS §4.1, §8)

### 3.1 Closed-Form Ridge Regression (`RidgeRegressor`)
SRS §4.1 specifies closed-form Ridge regularization:
$$\beta_s = (X^T X + \lambda I)^{-1} X^T y_s, \quad \hat{s} = \max(0, X \beta_s)$$

- Support `l2_penalty: float = 1.0` ($\lambda > 0$) and `fit_intercept: bool = True`:
  - Validate `l2_penalty > 0` (`ValueError` otherwise).
  - When `fit_intercept=True`:
    - Let $\bar{X} = \text{mean}(X, \text{axis}=0)$ and $\bar{y} = \text{mean}(y)$.
    - Center $X_c = X - \bar{X}$ and $y_c = y - \bar{y}$.
    - Solve $(X_c^T X_c + \lambda I_p) \beta = X_c^T y_c$ via `np.linalg.solve`.
    - Set `intercept_ = float(y_mean - X_mean @ beta)`.
    - *(Note: when $n = 0$, raise `ValueError`.)*
  - When `fit_intercept=False`:
    - Solve $(X^T X + \lambda I_p) \beta = X^T y$ via `np.linalg.solve`, and set `intercept_ = 0.0`.
  - `predict(X, *, clamp_nonnegative: bool = True) -> np.ndarray`:
    - Computes `raw = X @ self.coef_ + self.intercept_`.
    - If `clamp_nonnegative` is `True`, applies `np.maximum(0.0, raw)`.

### 3.2 Multi-Component Wrapper (`ComponentRidgeModel`) & `ScoringRescorer`
```python
class ComponentRidgeModel:
    def __init__(
        self,
        *,
        l2_penalty: float = 1.0,
        fit_intercept: bool = True,
        components: Sequence[str] = COMPONENTS,
    ) -> None: ...

    def fit(self, X: np.ndarray, y_by_component: Dict[str, np.ndarray]) -> "ComponentRidgeModel": ...

    def predict_components(self, x: Union[np.ndarray, Sequence[float]]) -> Dict[str, float]:
        """Predict clamped nonnegative scalar components for a single 1D feature vector x."""

    def predict_batch(self, X: np.ndarray) -> Dict[str, np.ndarray]:
        """Predict clamped nonnegative component arrays for a 2D feature matrix X."""


BonusFn = Callable[[Dict[str, float]], float]


class ScoringRescorer:
    """Maps predicted statistical components into league fantasy points (SRS §4.1)."""

    def __init__(
        self,
        stat_weights: Dict[str, float],
        *,
        bonus_fn: Optional[BonusFn] = None,
    ) -> None: ...

    def score(self, predicted_components: Dict[str, float]) -> float:
        """Compute sum_s (s_hat * weight_s) + bonus_fn(predicted_components)."""
```

---

## 4. Module 3 Specification: `fantasy/solver.py` (SRS §4.2, §8)

### 4.1 Mathematical Condition (SRS §4.2)
Let candidate streamer $A$ be on free agency, and candidate drop $D$ be on the active roster:
$$\Delta \text{Value} = \underbrace{\left(\widehat{\text{FP}}_{A, \text{Tonight}} + \sum_{t=1}^{T_{\text{week}}} \widehat{\text{FP}}_{A, t}\right)}_{\text{Streamer Total Weekly Points}} - \underbrace{\left(\widehat{\text{FP}}_{D, \text{Tonight}} + \sum_{t=1}^{T_{\text{week}}} \widehat{\text{FP}}_{D, t}\right)}_{\text{Drop Total Weekly Points}} + \underbrace{H \times \left( \overline{\text{FP}}_{A, \text{ROS}} - \overline{\text{FP}}_{D, \text{ROS}} \right)}_{\text{Long-Term Asset Depreciation Penalty}}$$
*(Note: for an off-night drop $D$ not playing tonight, $\widehat{\text{FP}}_{D, \text{Tonight}} = 0.0$, which reduces identically to SRS §4.2 Eq. 221.)*

### 4.2 Data Contracts & Decision Rules
```python
DEFAULT_HORIZON_WEEKS: float = 2.5
ACTION_RECOMMEND_SWAP = "RECOMMEND_SWAP"
ACTION_HOLD_ASSET = "HOLD_ASSET"


@dataclass(frozen=True)
class CandidatePlayer:
    player_id: int
    display_name: str
    position: str = "C"
    yahoo_player_id: Optional[str] = None
    fp_tonight: float = 0.0
    fp_remaining_week: Tuple[float, ...] = ()
    fp_ros_weekly_avg: float = 0.0
    is_droppable: bool = True

    @property
    def total_weekly_fp(self) -> float:
        return float(self.fp_tonight) + float(sum(self.fp_remaining_week))


@dataclass(frozen=True)
class SwapEvaluation:
    streamer: CandidatePlayer
    drop_candidate: CandidatePlayer
    horizon_weeks: float
    tonight_gain: float
    streamer_weekly_fp: float
    drop_weekly_fp: float
    short_term_gain: float
    asset_depreciation_penalty: float
    delta_value: float
    action: str          # "RECOMMEND_SWAP" or "HOLD_ASSET"
    explanation: str
```

- **Exact Decision & Formatting Rules (SRS §4.2):**
  - `tonight_gain = streamer.fp_tonight - drop_candidate.fp_tonight`
  - `short_term_gain = streamer.total_weekly_fp - drop_candidate.total_weekly_fp`
  - `asset_depreciation_penalty = horizon_weeks * (streamer.fp_ros_weekly_avg - drop_candidate.fp_ros_weekly_avg)`
  - `delta_value = short_term_gain + asset_depreciation_penalty`
  - If `delta_value > 0.0`:
    - `action = "RECOMMEND_SWAP"`
    - `explanation = f"Recommended swap: adding {streamer.display_name} for {drop_candidate.display_name} yields +{delta_value:.1f} net FP over the next {horizon_weeks:g} weeks."`
  - If `delta_value <= 0.0`:
    - `action = "HOLD_ASSET"`
    - `explanation = f"Short-term streaming gain of +{tonight_gain:.1f} FP tonight is rejected: dropping {drop_candidate.display_name} results in net loss of -{abs(delta_value):.1f} FP over the next {horizon_weeks:g} weeks."`

- **Required Functions:**
  1. `evaluate_swap(streamer: CandidatePlayer, drop_candidate: CandidatePlayer, *, horizon_weeks: float = DEFAULT_HORIZON_WEEKS) -> SwapEvaluation`
     - Raises `ValueError` if `horizon_weeks < 0` or `not drop_candidate.is_droppable`.
  2. `solve_best_stream(free_agents: Sequence[CandidatePlayer], roster: Sequence[CandidatePlayer], *, horizon_weeks: float = DEFAULT_HORIZON_WEEKS) -> Optional[SwapEvaluation]`
     - Filters `roster` to `is_droppable=True`. Returns `None` if `free_agents` or droppable `roster` is empty.
     - Evaluates all `(streamer, drop)` pairs and returns the `SwapEvaluation` with the maximum `delta_value` (tie-breaking by higher `tonight_gain`).

---

## 5. Required Unit Tests

### `tests/test_features.py`
1. `test_compute_ewma_weights_recent_games_more_heavily`: Verifies `compute_ewma([], 0.08) == 0.0`, single-element returns itself, and an increasing series `[1.0, 2.0, 5.0]` produces exact manual recursion.
2. `test_temporal_integrity_future_game_cannot_leak` (**SRS §8 Gate 1**):
   - Seeds 3 games for a skater on `2026-10-18`, `2026-10-20`, `2026-10-22` and computes `build_player_features` and `build_training_dataset` as of `2026-10-23`.
   - Then inserts a massive outlier game on `2026-10-23` and `2026-10-24` (e.g., `goals=10, ixg=9.5, icorsi=40`, plus opponent goalie `gsax=-8.0`).
   - Asserts `build_player_features(..., as_of_date="2026-10-23")` and `build_training_dataset("2026-10-23")` are bit-for-bit identical before and after inserting the `>= 2026-10-23` rows.
3. `test_opp_def_rtg_and_goalie_gsax_suppression`: Verifies soft opponent defense (`opp_def_rtg_rel > 0`) vs stingy defense (`opp_def_rtg_rel < 0`) and rolling opponent goalie `GSAx/60`.
4. `test_pp_toi_share_and_schedule_flags`: Verifies `pp_toi_share_ewma` in `[0, 1]`, `b2b_flag == 1.0` when `team_rest == 0`, and `rest_diff == team_rest - opp_rest`.

### `tests/test_ridge.py`
1. `test_closed_form_ridge_matches_reference_solution` (**SRS §8 Gate 1**):
   - Generates a deterministic synthetic design matrix $X$ (`np.random.default_rng(42)`) and target $y$.
   - Verifies `RidgeRegressor(l2_penalty=1.5, fit_intercept=True)` and `fit_intercept=False` match the explicit centered/uncentered normal equations $(X^T X + \lambda I)^{-1} X^T y$ within `1e-5` (and `1e-10`).
2. `test_predictions_clamped_nonnegative`: Verifies negative linear combinations are clamped to `0.0`.
3. `test_component_ridge_and_scoring_rescorer`: Fits `ComponentRidgeModel` on all 6 SRS components (`SOG, iXG, G, A, BLK, HIT`), predicts components for a test vector, and verifies `ScoringRescorer` produces exact weighted sum + bonus adjustment ( confirming the model predicts components first rather than aggregate fantasy points directly, SRS §4.1).

### `tests/test_solver.py`
1. `test_elite_asset_hold_enforced_with_exact_srs_message` (**SRS §8 Gate 1**):
   - Streamer $A$ has `fp_tonight=4.2`, `fp_remaining_week=(2.0,)`, `fp_ros_weekly_avg=10.0`.
   - Roster drop $D$ ("Evan Bouchard") is off tonight (`fp_tonight=0.0`), `fp_remaining_week=(3.0,)`, `fp_ros_weekly_avg=14.0`, `horizon_weeks=2.5`.
   - Short-term gain is $(4.2 + 2.0) - 3.0 = +3.2$, asset penalty is $2.5 \times (10.0 - 14.0) = -10.0$, so $\Delta \text{Value} = -6.8 \le 0$.
   - Asserts `action == "HOLD_ASSET"` and `explanation == "Short-term streaming gain of +4.2 FP tonight is rejected: dropping Evan Bouchard results in net loss of -6.8 FP over the next 2.5 weeks."`
2. `test_positive_delta_emits_recommend_swap`:
   - Streamer $A$ beats low-end roster spot $D$ even after $H$-week penalty; asserts `action == "RECOMMEND_SWAP"` and `delta_value > 0`.
3. `test_solve_best_stream_skips_undroppable_and_ranks_highest_delta`:
   - Verifies undroppable players (`is_droppable=False`) are never dropped and the solver selects the `(streamer, drop)` pair maximizing `delta_value`.

---

## 6. Verification Gate (Run Before Completing Session)
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
```
Both commands MUST pass with zero errors. Stage all new files with `git add` and leave uncommitted for Agent 1 review.
