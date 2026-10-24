# Runbook: Phase 5 — Automated Scheduler, Loopback API & Static Mobile PWA Dashboard

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Implement `scripts/` (allowlisted report exporter, `launchd` plist, `gh-pages` publish script), `api/` (loopback FastAPI server on `127.0.0.1:8877`), `web/` (roster-first mobile PWA + Node policy test suite), and `tests/test_delivery.py`.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_5_DELIVERY_AND_PWA.md` — This specification.
2. `fantasy/solver.py` — `CandidatePlayer`, `SwapEvaluation`, `ACTION_HOLD_ASSET`, `ACTION_RECOMMEND_SWAP`.
3. `betting/ev.py` — `BetRecommendation`, `MarketTotalLine`.

### Files to Create / Modify
- `scripts/__init__.py` (CREATE)
- `scripts/refresh.py` (CREATE)
- `scripts/com.puckluck.refresh.plist` (CREATE)
- `scripts/publish_gh_pages.sh` (CREATE)
- `api/__init__.py` (CREATE)
- `api/server.py` (CREATE)
- `web/index.html` (CREATE)
- `web/styles.css` (CREATE)
- `web/app.js` (CREATE)
- `web/manifest.webmanifest` (CREATE)
- `web/sw.js` (CREATE)
- `web/tests/dashboard.test.mjs` (CREATE)
- `tests/test_delivery.py` (CREATE)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, `betting/*`, existing `tests/test_*.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Subsystem 1: Allowlisted Static Export & Scheduler (`scripts/`) (SRS §6.2, §7.1, §7.2)

### 2.1 `scripts/refresh.py` — Public Report Contract & Security Boundary
`dist/report.json` is published to GitHub Pages (`gh-pages`), so it MUST enforce a strict allowlist and reject any secret keys or local filesystem paths.

- **Constants:**
  - `STALE_AFTER_HOURS: float = 26.0` (SRS §6.2.2)
  - `DEFAULT_REPORT_PATH = Path("dist/report.json")`
  - `TOP_LEVEL_ALLOWLIST: FrozenSet[str] = frozenset({"generated_at", "stale_after_hours", "team_summary", "roster", "recommendation", "games", "free_agents"})`
  - `ROSTER_ITEM_ALLOWLIST: FrozenSet[str] = frozenset({"player_id", "display_name", "position", "selected_slot", "team_abbr", "opponent_abbr", "opp_def_rtg_rel", "goalie_status", "projected_fp", "shots_on_goal", "ixg", "goals", "assists", "blocks", "hits"})`
  - `GAME_ITEM_ALLOWLIST: FrozenSet[str] = frozenset({"game_id", "start_time_utc", "home_abbr", "away_abbr", "status", "lambda_home", "lambda_away", "expected_total", "line", "over_odds", "under_odds", "prob_over", "prob_under", "best_side", "ev", "is_positive_ev", "home_goalie_status", "away_goalie_status"})`
  - `FREE_AGENT_ALLOWLIST: FrozenSet[str] = frozenset({"player_id", "display_name", "position", "team_abbr", "percent_owned", "projected_fp", "ixg_per60_ewma", "icorsi_per60_ewma", "pp_toi_share_ewma"})`
  - `RECOMMENDATION_ALLOWLIST: FrozenSet[str] = frozenset({"action", "explanation", "delta_value", "tonight_gain", "short_term_gain", "asset_depreciation_penalty", "horizon_weeks", "streamer_name", "drop_name"})`
  - `TEAM_SUMMARY_ALLOWLIST: FrozenSet[str] = frozenset({"team_name", "team_initials", "league_name", "adds_used", "adds_limit", "matchup_score"})`
  - `FORBIDDEN_KEY_SUBSTRINGS: Tuple[str, ...] = ("secret", "token", "password", "client_id", "authorization", "api_key", "env_path", "db_path")`

- **Required Functions:**
  1. `sanitize_public_report(raw: Dict[str, Any]) -> Dict[str, Any]`
     - Strips any key not in `TOP_LEVEL_ALLOWLIST` and its nested per-section allowlists (`TEAM_SUMMARY_ALLOWLIST`, `ROSTER_ITEM_ALLOWLIST`, `RECOMMENDATION_ALLOWLIST`, `GAME_ITEM_ALLOWLIST`, `FREE_AGENT_ALLOWLIST`).
  2. `validate_public_report(report: Dict[str, Any]) -> None`
     - Recursively inspects all keys and string values in `report`.
     - Raises `ValueError` if:
       - Any top-level or nested key is outside the allowlists, or contains any substring in `FORBIDDEN_KEY_SUBSTRINGS` (case-insensitive).
       - Any string value looks like a local filesystem path or secret leak (matches `/Users/`, `/home/`, `.sqlite3`, `.env`, `Bearer `, `Basic `).
       - `generated_at` is missing or not a non-empty string.
  3. `build_public_report(*, generated_at: Optional[str] = None, team_summary: Optional[Dict[str, Any]] = None, roster: Sequence[Dict[str, Any]] = (), recommendation: Optional[Union[SwapEvaluation, Dict[str, Any]]] = None, games: Sequence[Dict[str, Any]] = (), free_agents: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]`
     - Converts a `SwapEvaluation` (if passed) or dict into the allowlisted `recommendation` dict, applies `sanitize_public_report`, runs `validate_public_report`, and returns the clean dictionary.
  4. `write_report_json(report: Dict[str, Any], path: Union[str, Path] = DEFAULT_REPORT_PATH) -> Path`
     - Runs `validate_public_report(report)` first (never writes unvalidated payloads), then atomically writes formatted JSON via `.tmp` + `os.replace`.

### 2.2 `scripts/com.puckluck.refresh.plist` & `scripts/publish_gh_pages.sh` (SRS §7.2)
- `scripts/com.puckluck.refresh.plist`: Valid XML PropertyList configuring `Label` = `com.puckluck.refresh`, `ProgramArguments` running `.venv/bin/python -m scripts.refresh`, `StartCalendarInterval` (`Hour` = `6`, `Minute` = `0`), and `StandardOutPath` / `StandardErrorPath` at `data/logs/refresh.log`.
- `scripts/publish_gh_pages.sh`: POSIX shell script (`set -euo pipefail`) that verifies `dist/report.json` exists, stages only `web/` assets (`index.html`, `styles.css`, `app.js`, `manifest.webmanifest`, `sw.js`) and `dist/report.json`, and refuses to proceed if `.env` or `*.sqlite3` is present in the staging directory.

---

## 3. Subsystem 2: Local Loopback FastAPI Server (`api/server.py`) (SRS §7.1)

- **Constants:**
  - `DEFAULT_HOST = "127.0.0.1"`
  - `DEFAULT_PORT = 8877`
  - `ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})`
  - `ALLOWED_CORS_ORIGINS = ["http://127.0.0.1:8877", "http://localhost:8877", "http://127.0.0.1:3000", "http://localhost:3000"]`

- **Required Functions:**
  1. `validate_bind_host(host: str) -> str`:
     - Returns `host` if `host in ALLOWED_LOOPBACK_HOSTS`; raises `ValueError` otherwise (e.g. `"0.0.0.0"` or external IP is rejected per SRS §7.1).
  2. `create_app(*, report_path: Union[str, Path] = DEFAULT_REPORT_PATH, report_provider: Optional[Callable[[], Dict[str, Any]]] = None) -> FastAPI`:
     - Configures `CORSMiddleware` with `allow_origins=ALLOWED_CORS_ORIGINS`, `allow_methods=["GET"]`.
     - `GET /api/health` -> `{"status": "ok", "host": DEFAULT_HOST, "port": DEFAULT_PORT}`
     - `GET /api/report` -> loads report from `report_provider()` if provided, else reads `report_path` (or returns an empty valid report via `build_public_report()` if file does not exist yet), validates with `validate_public_report`, and returns JSON.

---

## 4. Subsystem 3: Mobile-First PWA Dashboard (`web/`) & Node Policy Suite (SRS §6.1, §6.2, §8 Gate 3)

### 4.1 Design System & Visual Tokens (`web/styles.css`, `web/index.html`)
1. **Palette (SRS §6.1.2 — exact hex codes must appear in `web/styles.css`):**
   - `--bg-base: #141619;` (Deep charcoal)
   - `--bg-surface: #1c2026;` (Ice-steel undertones)
   - `--accent-cyan: #4da6ff;` (Muted frost cyan)
   - Status badge tokens:
     - `CONFIRMED` starting goalie badge (subtle gold)
     - `PROJECTED` unconfirmed goalie badge (muted steel)
     - `RECOMMEND_ADD` / `RECOMMEND_SWAP` badge (emerald green)
     - `HOLD` / `HOLD_ASSET` badge (amber/plum)
2. **Roster-First Hierarchy & 4-Tab Fixed Bottom Navigation (SRS §6.1.1, §6.1.3):**
   - Compact top bar: team avatar/initials, swipeable/selectable league/team pill, transaction counter (`Adds Left: X/Y`), matchup score, and stale-data banner container.
   - Primary view above the fold defaults to **`01 Roster`**.
   - Fixed bottom navigation (`375px–430px` mobile-first breakpoint) with exact labels:
     - `01 Roster`
     - `02 Add / Drop`
     - `03 Game Totals`
     - `04 Free Agents`
3. **PWA Files (`web/manifest.webmanifest`, `web/sw.js`):**
   - `web/manifest.webmanifest`: valid JSON with `"name": "PuckLuck"`, `"short_name": "PuckLuck"`, `"display": "standalone"`, `"background_color": "#141619"`, `"theme_color": "#141619"`.
   - `web/sw.js`: Service worker caching shell assets (`./`, `./index.html`, `./styles.css`, `./app.js`, `./manifest.webmanifest`, `./report.json`) for offline standalone operation.

### 4.2 Pure Rendering & Policy Logic in `web/app.js`
Write `web/app.js` as an ES module (`export function ...`) that works both in the browser and when imported directly by `node --test web/tests/dashboard.test.mjs`:

1. `export const STALE_THRESHOLD_HOURS = 26;`
2. `export function isReportStale(generatedAtIso, nowMs = Date.now(), maxHours = STALE_THRESHOLD_HOURS)`
   - Returns `true` if `generatedAtIso` is missing/invalid or `(nowMs - Date.parse(generatedAtIso)) > maxHours * 3600 * 1000`.
3. `export function renderHeader(report, { nowMs = Date.now() } = {})`
   - Renders compact header with `team_initials`, `team_name`, `Adds Left: ${addsLeft}/${addsLimit}`, `matchup_score`, and when `isReportStale(report.generated_at, nowMs, report.stale_after_hours || 26)` is `true`, includes a prominent `STALE DATA` warning banner.
4. `export function renderRosterTab(roster = [])`
   - Renders active roster table/cards in slot order.
   - Displays `CONFIRMED` or `PROJECTED` badge when `goalie_status` is present.
   - Handles zero-shot / zero-point appearances (`shots_on_goal: 0`, `projected_fp: 0`) cleanly as `"0"` / `"0.0"` (never `NaN`, `null`, or `undefined`).
5. `export function renderAddDropTab(recommendation, { stale = false } = {})`
   - Renders the streaming solver card & expandable math drawer (`tonight_gain`, `short_term_gain`, `asset_depreciation_penalty`, `delta_value`, `explanation`).
   - Emits `RECOMMEND_ADD` badge when `action === "RECOMMEND_SWAP"` and `HOLD` badge when `action === "HOLD_ASSET"`.
   - When `stale === true`, adds class `stale-disabled` and `data-stale="true"` (graying out actionable recommendations with `STALE DATA` notice per SRS §6.2.2).
6. `export function renderGameTotalsTab(games = [])`
   - Sorts games chronologically by `start_time_utc`.
   - For `status === "POSTPONED"`, renders a `POSTPONED` badge and suppresses `+EV` edge tags.
   - For active games, displays `expected_total` vs `line`, goalie status badges (`CONFIRMED` / `PROJECTED`), and flags `+EV` badge only when `is_positive_ev === true` (`ev >= 0.035`).
7. `export function renderFreeAgentsTab(freeAgents = [], { query = "" } = {})`
   - Filters by case-insensitive `query` on `display_name` / `team_abbr` / `position` and ranks descending by `ixg_per60_ewma` (with `icorsi_per60_ewma` and `pp_toi_share_ewma` columns).
8. `export function renderDashboard(report, { nowMs = Date.now(), activeTab = "roster", query = "" } = {})`
   - Composes header + all 4 tab panels (with `01 Roster` active above the fold by default) and returns the complete HTML string. When `typeof document !== "undefined"`, binds tab switching, search input, and `fetch("./report.json")` on `DOMContentLoaded`.

---

## 5. Required Automated Tests

### 5.1 Python Suite: `tests/test_delivery.py`
1. `test_sanitize_and_validate_public_report_strips_unknown_and_blocks_secrets`:
   - Verifies `sanitize_public_report` strips non-allowlisted fields.
   - Verifies `validate_public_report` raises `ValueError` when a forbidden key (`refresh_token`, `client_secret`) or local file path (`/Users/mkodi/secret.sqlite3`, `.env`) is injected into the report dict.
2. `test_build_public_report_from_swap_evaluation_and_write_json`:
   - Passes a `SwapEvaluation` (`HOLD_ASSET`) into `build_public_report`, writes `tmp_path / "report.json"`, and verifies round-trip JSON content.
3. `test_validate_bind_host_enforces_loopback` (**SRS §7.1**):
   - Verifies `"127.0.0.1"` and `"localhost"` pass, while `"0.0.0.0"` and `"192.168.1.10"` raise `ValueError`.
4. `test_fastapi_health_and_report_endpoints`:
   - Inspects or invokes `create_app` endpoints (`/api/health` and `/api/report`) directly (via async route call or `TestClient` if socket-free, note: calling the route functions directly or inspecting `app.routes` / `CORSMiddleware` avoids any socket interaction) and verifies allowlisted report output and restricted CORS origins.
5. `test_launchd_plist_and_publish_script_contracts` (**SRS §7.2**):
   - Parses `scripts/com.puckluck.refresh.plist` with `plistlib.loads` and asserts `Label == "com.puckluck.refresh"` and `StartCalendarInterval == {"Hour": 6, "Minute": 0}`.

### 5.2 Frontend Policy Suite: `web/tests/dashboard.test.mjs` (`node --test web/tests/`)
1. `renders roster-first dashboard offline with required visual tokens`:
   - Verifies `web/styles.css` contains `#141619`, `#1c2026`, and `#4da6ff`, `web/manifest.webmanifest` has `display: "standalone"`, and `renderDashboard` places `01 Roster` as the primary active view above the fold with `Adds Left: 3/4`.
2. `enforces 26-hour stale data gate on add/drop recommendations` (**SRS §6.2.2**):
   - Verifies `isReportStale` returns `false` at 25 hours and `true` at 27 hours, and `renderDashboard` at 27 hours emits `STALE DATA` and marks the add/drop card `stale-disabled` / `data-stale="true"`.
3. `handles edge cases: postponed games, zero-shot appearances, and unconfirmed goalies` (**SRS §8 Gate 3**):
   - Verifies a `POSTPONED` game renders `POSTPONED` and suppresses `+EV` badges even if `is_positive_ev` was true.
   - Verifies a skater with `shots_on_goal: 0, projected_fp: 0` renders cleanly without `NaN` or `undefined`.
   - Verifies `CONFIRMED` vs `PROJECTED` goalie badges render accurately on both roster and game total cards.
4. `ranks and filters free agents by 15-game EWMA process metrics`:
   - Verifies `renderFreeAgentsTab` orders skaters by `ixg_per60_ewma` descending and filters by search query.

---

## 6. Verification Gate (All Three SRS §8 Suites Must Pass)
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/
```
All three commands MUST pass with zero errors. Stage all new files with `git add` and leave uncommitted for Agent 1 review.
