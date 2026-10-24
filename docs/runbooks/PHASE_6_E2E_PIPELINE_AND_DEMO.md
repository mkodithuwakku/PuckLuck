# Runbook: Phase 6 — End-to-End Warehouse Orchestration, Yahoo-Optional Mode & Live PWA Mount

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Upgrade `scripts/refresh.py` and `api/server.py` (plus `tests/test_delivery.py`) so the entire PuckLuck system runs end-to-end from SQLite warehouse → ML/Poisson engines → `dist/report.json` → live browser PWA at `http://127.0.0.1:8877/` **without requiring Yahoo Developer credentials**.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_6_E2E_PIPELINE_AND_DEMO.md` — This specification.
2. `scripts/refresh.py` — Current allowlist exporter & CLI entrypoint.
3. `api/server.py` — Current loopback FastAPI app.
4. `tests/test_delivery.py` — Current delivery test suite.

*(Note: All required signatures from `db.py`, `features/builder.py`, `models/ridge.py`, `fantasy/solver.py`, `betting/poisson.py`, and `betting/ev.py` are provided inline below so you do not need to read those files.)*

### Files to Modify
- `scripts/refresh.py` (MODIFY)
- `api/server.py` (MODIFY)
- `tests/test_delivery.py` (MODIFY)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, `betting/*`, `web/*`, other `tests/test_*.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Reference Signatures of Existing Engine Modules (Already Implemented)

```python
# db.py
db.init_db(db_path="data/puckluck.sqlite3") -> sqlite3.Connection
db.upsert_team(conn, team_id: int, abbreviation: str, team_name: str, venue: str, *, altitude_ft: int = 0) -> None
db.upsert_player(conn, player_id: int, display_name: str, primary_position: str, *, canonical_name: Optional[str] = None, yahoo_player_id: Optional[str] = None, shoots_catches: Optional[str] = None, birth_date: Optional[str] = None) -> None
db.insert_skater_game(conn, row: dict) -> None
db.insert_goalie_game(conn, row: dict) -> None

# features/builder.py
from features.builder import COMPONENTS, FEATURE_NAMES, FeatureBuilder
fb = FeatureBuilder(conn)
fv = fb.build_player_features(player_id, team_id, opponent_id, as_of_date, is_home=..., opponent_goalie_id=..., team_rest=..., opp_rest=...)
# fv.features: Dict[str, float] with keys:
#   "ixg_per60_ewma", "icorsi_per60_ewma", "ihdcf_per60_ewma", "pp_toi_share_ewma",
#   "toi_minutes_ewma", "opp_def_rtg_rel", "opp_goalie_gsax_per60", "rest_diff", "b2b_flag", "home_flag"
# fv.to_array() -> 1D np.ndarray
X, y_by_comp, sample_keys = fb.build_training_dataset(as_of_date, min_prior_games=1)

# models/ridge.py
from models.ridge import ComponentRidgeModel, ScoringRescorer
model = ComponentRidgeModel(l2_penalty=1.0).fit(X, y_by_comp)
pred_comps = model.predict_components(fv.to_array())  # Dict[str, float] for ("SOG", "iXG", "G", "A", "BLK", "HIT")
rescorer = ScoringRescorer(stat_weights)
fp = rescorer.score(pred_comps)

# fantasy/solver.py
from fantasy.solver import CandidatePlayer, SwapEvaluation, solve_best_stream

# betting/poisson.py & betting/ev.py
from betting.poisson import build_matchup_inputs_from_db, compute_expected_goals, price_total_goals
from betting.ev import MarketTotalLine, evaluate_total_market, upsert_total_line
```

---

## 3. Specification: `scripts/refresh.py` — End-to-End Warehouse Pipeline & Yahoo-Optional Demo Mode

Keep all existing constants and functions (`STALE_AFTER_HOURS`, `DEFAULT_REPORT_PATH`, `*_ALLOWLIST`, `FORBIDDEN_*`, `sanitize_public_report`, `validate_public_report`, `build_public_report`, `write_report_json`) intact and add:

### 3.1 Default League Weights (Used When Yahoo OAuth Is Not Configured)
```python
DEFAULT_STAT_WEIGHTS: Dict[str, float] = {
    "G": 3.0,
    "A": 2.0,
    "SOG": 0.4,
    "HIT": 0.5,
    "BLK": 0.5,
}
```

### 3.2 `seed_demo_warehouse(conn: sqlite3.Connection, *, as_of_date: str = "2026-10-23") -> None`
Deterministically populates an empty (or existing) SQLite warehouse so the full pipeline can be run and inspected offline without Yahoo or live network calls:
- Upserts at least 4 NHL teams into `dim_teams`:
  - `(25, "EDM", "Edmonton Oilers", "Rogers Place", altitude_ft=2208)`
  - `(21, "COL", "Colorado Avalanche", "Ball Arena", altitude_ft=5280)`
  - `(10, "TOR", "Toronto Maple Leafs", "Scotiabank Arena", altitude_ft=0)`
  - `(12, "CAR", "Carolina Hurricanes", "PNC Arena", altitude_ft=0)`
- Upserts at least 6 skaters and 2 goalies into `dim_players`:
  - Roster players (with `yahoo_player_id` set, e.g. `"449.p.6743"`): e.g. Connor McDavid (`8478402`, `"C"`), Nathan MacKinnon (`8477492`, `"C"`), Auston Matthews (`8479318`, `"C"`), Evan Bouchard (`8480803`, `"D"`), plus goalie Stuart Skinner (`8479979`, `"G"`, `yahoo_player_id="449.p.7979"`).
  - Free-agent skaters (`yahoo_player_id=None`): e.g. Zach Hyman (`8475786`, `"LW"`), Seth Jarvis (`8482093`, `"RW"`), Devon Toews (`8478038`, `"D"`).
  - Opposing goalie: Alexandar Georgiev (`8480382`, `"G"`, `yahoo_player_id=None`).
- Inserts at least 3 historical games per player strictly before `as_of_date` (e.g., `"2026-10-15"`, `"2026-10-18"`, `"2026-10-21"`) into `fact_skater_games` and `fact_goalie_games` with realistic `toi_seconds`, `pp_toi_seconds`, `goals`, `assists`, `shots_on_goal`, `blocks`, `hits`, `ixg`, `icorsi`, `ihdcf`, `gsax`.
- Inserts at least 2 market total lines via `upsert_total_line` (e.g. game `2026020101` for `COL @ EDM` at line `6.5` and game `2026020102` for `CAR @ TOR` at line `6.0`).
- Commits the transaction.

### 3.3 `run_warehouse_pipeline(...) -> Dict[str, Any]`
```python
def run_warehouse_pipeline(
    conn: sqlite3.Connection,
    *,
    as_of_date: str = "2026-10-23",
    stat_weights: Optional[Dict[str, float]] = None,
    team_summary: Optional[Dict[str, Any]] = None,
    horizon_weeks: float = 2.5,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
```
1. Uses `weights = stat_weights or DEFAULT_STAT_WEIGHTS` and `rescorer = ScoringRescorer(weights)`.
2. Instantiates `fb = FeatureBuilder(conn)` and builds training data:
   `X, y_by_comp, _ = fb.build_training_dataset(as_of_date, min_prior_games=1)`.
   - If `X.shape[0] >= 2`, fits `model = ComponentRidgeModel(l2_penalty=1.0).fit(X, y_by_comp)`.
   - Otherwise `model = None` (fallback: estimate components from player's latest historical game averages).
3. Queries all players in `dim_players` along with their most recent `team_id` and `opponent_id` from `fact_skater_games` / `fact_goalie_games` strictly before `as_of_date` (and team abbreviations from `dim_teams`).
4. For each skater with prior history:
   - Computes `fv = fb.build_player_features(pid, team_id, opp_id, as_of_date, is_home=True)`.
   - Computes `comps = model.predict_components(fv.to_array())` (rounded to 2 decimals, or integer-rounded for display where appropriate) and `proj_fp = round(rescorer.score(comps), 2)`.
   - Separates skaters with `yahoo_player_id IS NOT NULL` into `roster` items (assigning `selected_slot = primary_position`, and marking the lowest-ROS skater as `is_droppable=True` and top elite skaters as `is_droppable=True` so `solve_best_stream` evaluates them) and skaters with `yahoo_player_id IS NULL` into `free_agents` items (with `ixg_per60_ewma`, `icorsi_per60_ewma`, `pp_toi_share_ewma`, `projected_fp`, `percent_owned`).
5. For each rostered goalie (`primary_position == "G"` and `yahoo_player_id IS NOT NULL`):
   - Appends a roster item with `selected_slot="G"`, `goalie_status="CONFIRMED"`, and `projected_fp`.
6. Runs `recommendation = solve_best_stream(fa_candidates, roster_candidates, horizon_weeks=horizon_weeks)`.
7. Builds nightly `games` list from distinct matchup pairs in `fact_market_lines` (or distinct team pairs in the warehouse):
   - Uses `build_matchup_inputs_from_db`, `compute_expected_goals`, `price_total_goals`, and `evaluate_total_market` to populate `lambda_home`, `lambda_away`, `expected_total`, `line`, `over_odds`, `under_odds`, `prob_over`, `prob_under`, `best_side`, `ev`, `is_positive_ev`, `home_goalie_status="CONFIRMED"`, `away_goalie_status="PROJECTED"`.
8. Calls and returns `build_public_report(generated_at=generated_at, team_summary=summary, roster=roster_items, recommendation=recommendation, games=game_items, free_agents=fa_items)`.

### 3.4 CLI Entrypoint `main(argv: Optional[Sequence[str]] = None) -> Path`
- Parses optional CLI flags (`argparse`):
  - `--db-path` (default `os.environ.get("PUCKLUCK_DB_PATH", "data/puckluck.sqlite3")`)
  - `--output` (default `str(DEFAULT_REPORT_PATH)`)
  - `--date` (default `"2026-10-23"`)
  - `--demo` (action `"store_true"`: seeds demo warehouse via `seed_demo_warehouse(conn, as_of_date=args.date)` before running the pipeline; also auto-seeds if the database has 0 rows in `dim_players` so a fresh launch always produces a populated dashboard).
- Opens `conn = db.init_db(args.db_path)`, runs `run_warehouse_pipeline(conn, as_of_date=args.date)`, writes the output via `write_report_json(report, args.output)`, closes `conn`, and returns the output `Path`.

---

## 4. Specification: `api/server.py` — Serve `/report.json` & Static PWA (`web/`)

Update `create_app` in `api/server.py`:
1. Add optional parameter `web_dir: Optional[Union[str, Path]] = Path("web")`.
2. Register `GET /report.json` (in addition to `GET /api/report`) calling the exact same validated report handler so `web/app.js`'s `fetch("./report.json")` works seamlessly when browsing `http://127.0.0.1:8877/`.
3. When `report_provider is None` and `not path.is_file()`, instead of returning an empty report, `GET /api/report` and `GET /report.json` should generate a populated report on the fly (or initialize & write `path` via an in-memory demo pipeline) so starting `uvicorn` on a clean checkout immediately serves a working dashboard.
4. If `web_dir` exists and is a directory, mount `fastapi.staticfiles.StaticFiles(directory=str(web_dir), html=True)` at `"/"` **after** registering `/api/health`, `/api/report`, and `/report.json`.

---

## 5. Required Unit Tests in `tests/test_delivery.py`

Keep all 6 existing tests in `tests/test_delivery.py` and add:
1. `test_end_to_end_demo_pipeline_populates_all_sections(tmp_path: Path)`:
   - Runs `refresh.main(["--db-path", str(tmp_path / "wh.sqlite3"), "--output", str(tmp_path / "report.json"), "--demo"])`.
   - Reads the written `report.json`, runs `validate_public_report(report)`, and asserts:
     - `len(report["roster"]) >= 4` (including at least one goalie with `goalie_status == "CONFIRMED"`).
     - `len(report["free_agents"]) >= 2` (with `ixg_per60_ewma > 0`).
     - `report["recommendation"]["action"] in {"RECOMMEND_SWAP", "HOLD_ASSET"}`.
     - `len(report["games"]) >= 2` (with `expected_total > 0` and `line > 0`).
2. `test_fastapi_serves_report_json_and_mounts_web_static(tmp_path: Path)`:
   - Verifies `create_app` exposes `/report.json` returning the same allowlisted payload as `/api/report`, and verifies the root `/` static mount is registered when `web/` exists.

---

## 6. Verification Gate (All Three SRS §8 Suites Must Pass)
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/dashboard.test.mjs
```
All three commands MUST pass with zero errors. Stage all modified files with `git add` and leave uncommitted for Agent 1 review.
