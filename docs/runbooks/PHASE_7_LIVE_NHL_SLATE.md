# Runbook: Phase 7 — Live NHL Slate Sync & Real-Life Daily Player Projections

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Add `sync_live_nhl_slate(...)` and `--live` CLI support to `scripts/refresh.py` (plus offline tests with stubbed fetchers in `tests/test_delivery.py`) so PuckLuck ingests the real-life NHL schedule, rosters, recent boxscores, and MoneyPuck metrics for teams playing on `target_date`, and projects fantasy points specifically for players on tonight's active NHL slate.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_7_LIVE_NHL_SLATE.md` — This specification.
2. `scripts/refresh.py` — Current warehouse pipeline & CLI entrypoint.
3. `ingestion/nhl.py` — `ScheduledGame`, `fetch_schedule`, `ingest_team_roster`, `ingest_boxscore`, `default_json_fetcher`.
4. `ingestion/moneypuck.py` — `update_cache`, `load_skaters`, `load_goalies`, `fetch_csv`, `season_for_date`.

### Files to Modify
- `scripts/refresh.py` (MODIFY)
- `tests/test_delivery.py` (MODIFY)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, `betting/*`, `api/*`, `web/*`, other `tests/test_*.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Specification: Live NHL + MoneyPuck Sync in `scripts/refresh.py`

### 2.1 `extract_moneypuck_metrics(skaters_df: pd.DataFrame, goalies_df: pd.DataFrame) -> Dict[int, Dict[str, float]]`
Converts cached MoneyPuck season-summary DataFrames (`situation == "all"` when the `situation` column is present) into per-game average process metrics keyed by `int(playerId)` for `ingest_boxscore(..., advanced_metrics=...)`:
- For each skater row (`games_played = max(1, int(row.get("games_played", 1) or 1))`):
  - `"ixg"`: `float(row.get("I_F_xGoals", 0.0)) / games_played`
  - `"icorsi"`: `int(round(float(row.get("I_F_shotAttempts", 0.0)) / games_played))`
  - `"ihdcf"`: `int(round(float(row.get("I_F_highDangerShots", 0.0)) / games_played))`
  - `"on_ice_xg_share"`: `float(row.get("onIce_xGoalsPercentage", 0.5) or 0.5)`
- For each goalie row (`games_played = max(1, int(row.get("games_played", 1) or 1))`):
  - `"gsax"`: `float(row.get("goalsSavedAboveExpected", 0.0)) / games_played`
  - `"hd_save_pct"`: `float(row.get("savePercentage", 0.900) or 0.900)`

### 2.2 `sync_live_nhl_slate(...) -> List[ScheduledGame]`
```python
def sync_live_nhl_slate(
    conn: sqlite3.Connection,
    target_date: str,
    *,
    lookback_days: int = 7,
    nhl_fetcher: JsonFetcher = default_json_fetcher,
    mp_fetcher: Fetcher = fetch_csv,
    cache_dir: Union[str, Path] = Path("data/cache/moneypuck"),
    default_total_line: float = 6.5,
) -> List[ScheduledGame]:
```
1. Refreshes MoneyPuck cache (`moneypuck.update_cache(cache_dir=cache_dir, season=season_for_date(date.fromisoformat(target_date)), fetcher=mp_fetcher)`) and builds `adv_metrics = extract_moneypuck_metrics(load_skaters(...), load_goalies(...))` (if `mp_fetcher` or parsing fails at runtime, logs a warning and falls back to `adv_metrics = {}`).
2. Fetches today's slate via `slate = fetch_schedule(target_date, fetcher=nhl_fetcher)`.
3. For each unique team abbreviation (`home_abbr`, `away_abbr`) on today's `slate`:
   - Calls `ingest_team_roster(conn, abbr, fetcher=nhl_fetcher, commit=False)` so all active NHL players on tonight's slate are in `dim_players`.
   - Upserts both teams into `dim_teams` via `db.upsert_team` so tonight's matchups are registered even before tonight's boxscores exist.
4. Iterates `d` from `1` to `lookback_days` days strictly before `target_date` (`prev_date = (date.fromisoformat(target_date) - timedelta(days=d)).isoformat()`):
   - Calls `prior_games = fetch_schedule(prev_date, fetcher=nhl_fetcher)`.
   - For each prior game whose `game_id` is not already present in `fact_skater_games`:
     - Calls `ingest_boxscore(conn, g.game_id, fetcher=nhl_fetcher, advanced_metrics=adv_metrics, commit=False)`.
5. If no `yahoo_player_id` bindings exist in `dim_players` yet (Yahoo OAuth not configured):
   - Selects the top 6 skaters by historical TOI/xG and top 1 goalie among teams playing on `slate` and sets a synthetic local roster tag (`yahoo_player_id = f"local.p.{player_id}"`) so `01 Roster` and `02 Add / Drop` are populated with real players playing tonight, while the remaining skaters on tonight's slate populate `04 Free Agents`.
6. For each `ScheduledGame` `g` in `slate`:
   - If `g.game_id` has no row in `fact_market_lines`, seeds a baseline `MarketTotalLine(game_id=g.game_id, sportsbook="consensus", line=default_total_line, over_odds=-110, under_odds=-110, captured_at=f"{target_date}T12:00:00Z")` via `upsert_total_line(conn, ..., commit=False)` so `03 Game Totals` prices every real game on tonight's slate.
7. Commits and returns `slate`.

### 2.3 Active-Slate Filtering in `run_warehouse_pipeline` & CLI `--live` Flag
- Add optional parameter `scheduled_games: Optional[Sequence[ScheduledGame]] = None` to `run_warehouse_pipeline`:
  - When `scheduled_games` is non-empty:
    - Build a lookup mapping `team_id -> (opponent_id, is_home, start_time_utc)` from `scheduled_games`.
    - Only project skaters/goalies whose `team_id` is playing on `scheduled_games` (or who are on the active `roster`), using tonight's actual `opponent_id` and `is_home` from `scheduled_games`!
    - Build `games` in the report directly from `scheduled_games` (using each game's real `home_team_id`, `away_team_id`, and `start_time_utc`).
- In `main(argv)`:
  - Add `--live` (`action="store_true"`, help="Fetch live NHL schedule, rosters, recent boxscores, and MoneyPuck data for --date").
  - Change `--date` default to `date.today().isoformat()`.
  - When `--live` is passed, call `slate = sync_live_nhl_slate(conn, args.date, lookback_days=args.lookback_days)` (with `--lookback-days` default `5`) and pass `scheduled_games=slate` into `run_warehouse_pipeline`.

---

## 3. Required Unit Tests in `tests/test_delivery.py`

Add `test_sync_live_nhl_slate_projects_tonights_real_matchups(tmp_path: Path)`:
- Uses stubbed `nhl_fetcher` and `mp_fetcher` (100% offline, sockets blocked by `conftest.py`) serving:
  - Prior schedule & boxscore on `2026-10-22`
  - Tonight's schedule on `2026-10-23` (`COL @ EDM`)
  - Team rosters for `EDM` and `COL`
  - Canned MoneyPuck skaters & goalies CSVs
- Calls `slate = refresh.sync_live_nhl_slate(conn, "2026-10-23", lookback_days=1, nhl_fetcher=..., mp_fetcher=..., cache_dir=tmp_path)` followed by `report = refresh.run_warehouse_pipeline(conn, as_of_date="2026-10-23", scheduled_games=slate)`.
- Verifies `validate_public_report(report)` passes, `report["roster"]` and `report["free_agents"]` contain players from `EDM`/`COL` with `opponent_abbr` matching tonight's opponent, and `report["games"]` contains the `COL @ EDM` matchup with `start_time_utc` from the schedule.

---

## 4. Verification Gate
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/dashboard.test.mjs
```
Stage all modified files with `git add` and leave uncommitted for Agent 1 review.
