# Runbook: Phase 7A — Live NHL Slate Rollover, Open-Source Consensus Lines, Two-Team Fantasy & Point Scorers Backend

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Upgrade `scripts/refresh.py` and `tests/test_delivery.py` to support:
1. **Late-Night Slate Rollover:** When today's games are already finished (`FINAL`/`OFF`), automatically roll forward to the next date with upcoming games while ingesting tonight's completed boxscores into the historical warehouse.
2. **Open-Source Consensus Lines:** Pull unauthenticated public consensus Over/Under lines & odds (ESPN public NHL scoreboard API / NHL schedule odds) and rank the best picks for the slate.
3. **Two Yahoo Fantasy Teams (`teams` array):** Connect each fantasy team (`team_id` / `team_name`) to its own `roster`, `recommendation` ($H$-week add/drop), and `waiver_targets`.
4. **Point Scorers & Goal Scorers (`point_scorers` array):** Compute Poisson $1+$ Point probability ($1 - e^{-(\hat{G}+\hat{A})}$) and Anytime Goal probability ($1 - e^{-\hat{G}}$) for all skaters playing on the upcoming slate.

**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_7_LIVE_NHL_SLATE.md` — This specification.
2. `scripts/refresh.py` — Current warehouse pipeline & allowlist validator.
3. `ingestion/nhl.py` — `ScheduledGame`, `fetch_schedule`, `ingest_team_roster`, `ingest_boxscore`, `default_json_fetcher`.
4. `ingestion/moneypuck.py` — `update_cache`, `load_skaters`, `load_goalies`, `fetch_csv`, `season_for_date`.
5. `tests/test_delivery.py` — Current delivery tests.

### Files to Modify
- `scripts/refresh.py` (MODIFY)
- `tests/test_delivery.py` (MODIFY)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, `betting/*`, `api/*`, `web/*`, other `tests/test_*.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Allowlist Schema Additions in `scripts/refresh.py`

Extend the allowlists in `scripts/refresh.py` so the new multi-team and point-scorer sections pass `sanitize_public_report` and `validate_public_report`:

- Add `"active_slate_date"`, `"teams"`, and `"point_scorers"` to `TOP_LEVEL_ALLOWLIST`.
- Define `POINT_SCORER_ALLOWLIST: FrozenSet[str]`:
  ```python
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
          "fair_point_odds",
          "fair_goal_odds",
      }
  )
  ```
- Define `FANTASY_TEAM_ENTRY_ALLOWLIST: FrozenSet[str]`:
  ```python
  FANTASY_TEAM_ENTRY_ALLOWLIST: FrozenSet[str] = frozenset(
      {
          "team_key",
          "team_summary",
          "roster",
          "recommendation",
          "waiver_targets",
      }
  )
  ```
  where inside each item of `report["teams"]`:
  - `team_key` is a string (e.g. `"team_1"`, `"team_2"`, or Yahoo team key),
  - `team_summary` is filtered by `TEAM_SUMMARY_ALLOWLIST`,
  - `roster` is filtered by `ROSTER_ITEM_ALLOWLIST`,
  - `recommendation` is filtered by `RECOMMENDATION_ALLOWLIST`,
  - `waiver_targets` is filtered by `FREE_AGENT_ALLOWLIST`.
- Keep top-level `team_summary`, `roster`, `recommendation`, `free_agents` populated from `teams[0]` so all existing callers and tests remain 100% compatible.
- Update `_ALLOWED_KEYS_ALL`, `sanitize_public_report`, `validate_public_report`, and `build_public_report` to sanitize and validate `active_slate_date`, `teams`, and `point_scorers`.

---

## 3. Late-Night Slate Rollover & Open-Source Consensus Lines

### 3.1 `resolve_active_slate(request_date: str, *, nhl_fetcher: JsonFetcher = default_json_fetcher) -> Tuple[str, List[ScheduledGame], List[str]]`
Because users often run PuckLuck late at night after today's games have ended (`gameState` in `{"FINAL", "OFF"}`):
1. Fetch `{NHL_API_BASE}/schedule/{request_date}` via `nhl_fetcher`.
2. Inspect `gameWeek` (a list of `{"date": "YYYY-MM-DD", "games": [...]}` dicts):
   - A game dict `g` is considered **finished** if `str(g.get("gameState") or "").upper() in {"FINAL", "OFF"}`.
   - A day has **upcoming/active games** if `games` is non-empty and at least one game is NOT finished (`FUT`, `PRE`, `LIVE`, `CRIT`, or `gameState` omitted in test fixtures).
3. Starting from `request_date` and scanning forward through `gameWeek` (and if none found in `gameWeek`, checking `payload.get("nextStartDate")` or scanning backward in `gameWeek` to the most recent date with games if the whole week is finished/offseason):
   - Pick the first date ` >= request_date` with upcoming/active games as `active_slate_date`.
   - If `request_date`'s games are all `FINAL`/`OFF` and a later date in `gameWeek` has upcoming games, `active_slate_date` rolls forward to that next date, and `request_date` is included in `completed_dates` so tonight's finished boxscores are ingested into the warehouse!
4. Return `(active_slate_date, scheduled_games_for_active_date, completed_dates_before_active)`.

### 3.2 `fetch_open_consensus_lines(slate_date: str, scheduled_games: Sequence[ScheduledGame], *, http_fetcher: Callable[[str], Dict[str, Any]] = default_json_fetcher, default_line: float = 6.5) -> List[MarketTotalLine]`
Fetches open-source consensus Over/Under lines for `scheduled_games`:
- Queries public ESPN NHL scoreboard JSON (`https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={YYYYMMDD}`) via `http_fetcher` (wrapping any exception in a graceful fallback so offline/unavailable odds never crash the pipeline).
- Extracts `events[*].competitions[*].odds[*]` (`overUnder`, `overOdds`, `underOdds`, `provider.name`) matched by `(home_abbr, away_abbr)`.
- For any `ScheduledGame` without a live odds entry, creates a baseline `MarketTotalLine(game_id=g.game_id, sportsbook="consensus", line=default_line, over_odds=-110, under_odds=-110, captured_at=f"{slate_date}T12:00:00Z")`.

---

## 4. Point Scorers & Two-Team Fantasy Pipeline

### 4.1 Point Scorer Probabilities (`compute_point_scorer_projection`)
For each skater playing on `active_slate_date`:
- Given component predictions `comps` (`G`, `A`, `SOG`, `iXG`) and EWMA features `feats`:
  - `lam_g = max(0.01, float(comps.get("G", 0.0)))`
  - `lam_a = max(0.01, float(comps.get("A", 0.0)))`
  - `lam_pts = lam_g + lam_a`
  - `prob_point = round(1.0 - math.exp(-lam_pts), 4)` ($P(\text{Points} \ge 1)$)
  - `prob_goal = round(1.0 - math.exp(-lam_g), 4)` ($P(\text{Goals} \ge 1)$)
  - Convert `prob_point` and `prob_goal` into fair American odds (`prob_to_fair_american(p: float) -> int`: if `p >= 0.5`, `-int(round(100.0 * p / max(1e-6, 1.0 - p)))`; else `+int(round(100.0 * (1.0 - p) / max(1e-6, p)))`).
- Sort `point_scorers` descending by `prob_point` (tie-break by `prob_goal`).

### 4.2 Two Fantasy Teams (`teams` array in `run_warehouse_pipeline` & `seed_demo_warehouse`)
- In `seed_demo_warehouse`:
  - Tag 4 skaters + 1 goalie with `yahoo_player_id = "team1.p.<id>"` (Team 1: `"McDavid's Oil"`, `"High-Stakes H2H"`) and another 4 skaters + 1 goalie with `yahoo_player_id = "team2.p.<id>"` (Team 2: `"MacKinnon's Avs"`, `"Keeper Dynasty"`), leaving the remaining skaters as `yahoo_player_id = None` (waiver pool).
- In `run_warehouse_pipeline`:
  - Partition rostered players by team prefix (`"team1."` vs `"team2."`, or if standard `"449.p."` tags are present, split evenly or group by configured teams so there are always **2 fantasy team entries** in `report["teams"]`).
  - For each of the 2 teams:
    - Compute its `roster` list, run `solve_best_stream(fa_candidates, team_roster_candidates, horizon_weeks=horizon_weeks)` for its `recommendation`, and attach `waiver_targets = fa_items`.
- In `sync_live_nhl_slate`:
  - Use `resolve_active_slate(target_date, nhl_fetcher=nhl_fetcher)` to determine `active_slate_date`, ingest completed games from the lookback window (including `target_date` itself if `target_date` already finished late at night!), ingest rosters for teams on `active_slate_date`, fetch consensus lines via `fetch_open_consensus_lines`, and when no Yahoo OAuth is configured, seed two distinct local fantasy teams (`team1.p.<id>` and `team2.p.<id>`) from players active on `active_slate_date`.

---

## 5. Required Unit Tests in `tests/test_delivery.py`

Keep all existing tests passing and add:
1. `test_late_night_rollover_advances_to_next_slate_and_ingests_finished_today`:
   - Stubs `/schedule/2026-10-23` where `2026-10-23` games all have `"gameState": "OFF"` (finished tonight) and `2026-10-24` has upcoming games (`"gameState": "FUT"`).
   - Verifies `resolve_active_slate("2026-10-23", ...)` returns `active_slate_date == "2026-10-24"` and includes `"2026-10-23"` in completed dates to ingest.
2. `test_two_fantasy_teams_and_point_scorers_in_report(tmp_path: Path)`:
   - Runs the pipeline and verifies:
     - `len(report["teams"]) == 2`, each with its own `team_summary`, `roster`, `recommendation`, and `waiver_targets`.
     - `len(report["point_scorers"]) >= 4`, sorted by `prob_point` descending, with `0 < sc["prob_goal"] <= sc["prob_point"] < 1.0` and valid `fair_point_odds` / `fair_goal_odds`.
3. `test_sync_live_nhl_slate_projects_tonights_real_matchups(tmp_path: Path)`:
   - Verifies offline-stubbed `sync_live_nhl_slate` + `run_warehouse_pipeline` produces valid `teams`, `games` (with consensus lines), and `point_scorers` for the active slate.

---

## 6. Verification Gate
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/dashboard.test.mjs
```
Stage all modified files with `git add` and leave uncommitted for Agent 1 review.
