# Runbook: Phase 8 — Bi-Weekly Walk-Forward Backtesting, Self-Tuner & Sleep-Proof Cloud Automation

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Implement `models/backtest.py`, wire `data/model_params.json` + intraday line auto-refresh into `scripts/refresh.py` and `api/server.py`, create sleep-proof GitHub Actions workflows (`.github/workflows/refresh.yml` and `.github/workflows/backtest.yml`) + update `scripts/com.puckluck.refresh.plist` (`RunAtLoad = true`), and add unit tests in `tests/test_backtest.py`.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_8_BACKTEST_AND_CLOUD_CRON.md` — This specification.
2. `features/builder.py` — `FeatureBuilder`, `COMPONENTS`, `EWMA_ALPHA`.
3. `models/ridge.py` — `ComponentRidgeModel`, `ScoringRescorer`.
4. `betting/poisson.py` — `build_matchup_inputs_from_db`, `compute_expected_goals`, `price_total_goals`, `DEFAULT_BASE_RATE`.
5. `scripts/refresh.py` — Report allowlists & pipeline entrypoint.

### Files to Create / Modify
- `models/backtest.py` (CREATE)
- `tests/test_backtest.py` (CREATE)
- `.github/workflows/refresh.yml` (CREATE)
- `.github/workflows/backtest.yml` (CREATE)
- `scripts/com.puckluck.refresh.plist` (MODIFY — set `RunAtLoad` to `<true/>` and add `StartInterval`)
- `scripts/refresh.py` (MODIFY — load tuned params from `data/model_params.json`, include `model_health` allowlist section, and add `--backtest` CLI flag)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/ridge.py`, `fantasy/*`, `betting/*`, `web/*`, existing `tests/test_*.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Specification: `models/backtest.py` — Walk-Forward Evaluator & Hyperparameter Self-Tuner

### 2.1 Candidate Hyperparameter Grid & Persisted Config (`data/model_params.json`)
```python
DEFAULT_MODEL_PARAMS_PATH = Path("data/model_params.json")
CANDIDATE_EWMA_ALPHAS: Tuple[float, ...] = (0.05, 0.08, 0.12, 0.16)
CANDIDATE_RIDGE_LAMBDAS: Tuple[float, ...] = (0.25, 0.5, 1.0, 2.5, 5.0)
CANDIDATE_BASE_RATES: Tuple[float, ...] = (2.90, 3.00, 3.05, 3.10, 3.20)


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
    component_mae: Dict[str, float]          # per-component out-of-sample MAE
    fantasy_point_mae: float                 # out-of-sample FP MAE
    totals_brier_score: float                # Brier score on Over/Under game totals
    recommended_params: TunedModelParams
    suggestions: Tuple[str, ...]             # human-readable tuning recommendations
    improved_over_baseline: bool
```

### 2.2 Required Functions in `models/backtest.py`
1. `load_model_params(path: Union[str, Path] = DEFAULT_MODEL_PARAMS_PATH) -> TunedModelParams`
   - Loads JSON from `path` if present, otherwise returns default `TunedModelParams()`.
2. `save_model_params(params: TunedModelParams, path: Union[str, Path] = DEFAULT_MODEL_PARAMS_PATH) -> Path`
   - Atomically writes `params` to `path` via `.tmp` + `os.replace`.
3. `run_walk_forward_backtest(conn: sqlite3.Connection, *, as_of_date: str, window_days: int = 14, stat_weights: Optional[Dict[str, float]] = None) -> BacktestReport`
   - Evaluates strictly out-of-sample walk-forward predictions over distinct `game_date` values in `[as_of_date - window_days, as_of_date)`:
     - For each evaluation date `d` in the window, trains only on rows with `game_date < d` (strict temporal integrity, SRS §8) and predicts games on `d`.
   - Grid-searches `(alpha, l2_penalty)` across `CANDIDATE_EWMA_ALPHAS × CANDIDATE_RIDGE_LAMBDAS` to minimize out-of-sample `fantasy_point_mae`, and grid-searches `base_rate` across `CANDIDATE_BASE_RATES` to minimize `totals_brier_score`.
   - Generates concrete human-readable `suggestions` (e.g., `"Decrease EWMA alpha from 0.08 to 0.05 (reduces FP MAE by 4.2%)"` or `"Retain baseline parameters (no out-of-sample improvement over window)"`).

---

## 3. Sleep-Proof Cloud Workflows (`.github/workflows/`) & `scripts/refresh.py` Integration

1. **`.github/workflows/refresh.yml`:**
   - Triggers on `schedule` (`cron: '30 12,17,22 * * *'` — morning refresh + pre-game line/pick refreshes in the cloud even when Mac is asleep) and `workflow_dispatch`.
   - Steps: `actions/checkout@v4`, `actions/setup-python@v5` (`python-version: '3.11'`), `actions/cache@v4` (caching `data/`), `pip install -r requirements.txt`, `python -m scripts.refresh --live`, and deploys `web/` + `dist/report.json` to `gh-pages`.
2. **`.github/workflows/backtest.yml`:**
   - Triggers bi-weekly on `schedule` (`cron: '0 11 1,15 * *'`) and `workflow_dispatch`.
   - Runs `python -m scripts.refresh --live --backtest`, persists updated `data/model_params.json` in cache, and deploys updated `dist/report.json` to `gh-pages`.
3. **`scripts/com.puckluck.refresh.plist`:**
   - Set `<key>RunAtLoad</key><true/>` so waking the Mac immediately runs a catch-up refresh.
4. **`scripts/refresh.py`:**
   - Add `"model_health"` to `TOP_LEVEL_ALLOWLIST` with `MODEL_HEALTH_ALLOWLIST = frozenset({"window_start", "window_end", "samples_evaluated", "games_evaluated", "fantasy_point_mae", "totals_brier_score", "ewma_alpha", "ridge_l2_penalty", "poisson_base_rate", "suggestions", "improved_over_baseline"})`.
   - Load tuned parameters via `load_model_params()` in `run_warehouse_pipeline` (using `params.ewma_alpha`, `params.ridge_l2_penalty`, `params.poisson_base_rate`).
   - Add `--backtest` CLI flag to `main(argv)` that runs `run_walk_forward_backtest`, saves improved params via `save_model_params`, and attaches `model_health` to the exported report.

---

## 4. Required Unit Tests (`tests/test_backtest.py`)

1. `test_walk_forward_backtest_evaluates_strictly_out_of_sample_and_suggests_tuning`:
   - Seeds multi-date synthetic games into an in-memory warehouse, runs `run_walk_forward_backtest`, and verifies `samples_evaluated > 0`, `fantasy_point_mae >= 0`, `0.0 <= totals_brier_score <= 1.0`, `recommended_params.ewma_alpha in CANDIDATE_EWMA_ALPHAS`, and non-empty `suggestions`.
2. `test_save_and_load_model_params_round_trip(tmp_path: Path)`:
   - Verifies atomic write and reload of `TunedModelParams`, and verifies `refresh.main(["--demo", "--backtest", ...])` populates `report["model_health"]` passing `validate_public_report`.
3. `test_github_actions_workflows_and_wake_catchup_plist_exist`:
   - Verifies `.github/workflows/refresh.yml` and `.github/workflows/backtest.yml` exist and contain `schedule:` / `cron:` triggers, `actions/cache`, and `gh-pages` deployment, and `com.puckluck.refresh.plist` has `RunAtLoad == True`.

---

## 5. Verification Gate
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/dashboard.test.mjs
```
Stage all modified/created files with `git add` and leave uncommitted for Agent 1 review.
