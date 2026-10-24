# Runbook: Phase 7B — Two-Team Connected UI, Point Scorers Tab & Live Auto-Refresh Polling

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Update `web/app.js`, `web/styles.css`, and `web/tests/dashboard.test.mjs` to:
1. Add a **2-Team Fantasy Switcher** in the header (`report.teams`) that switches both **`01 Roster`** and **`02 Add / Drop`** (including each team's connected `waiver_targets` search/list).
2. Replace `04 Free Agents` in the bottom navigation with **`04 Point Scorers`** (`renderPointScorersTab`), displaying likely Point Getters (`P(1+ Pt)`), Goal Scorers (`P(Goal)`), and Assist Getters (`P(Assist)`), their consensus market odds (`market_point_odds`, `market_goal_odds`, `market_assist_odds`), and flagging `+EV` profit opportunities (`is_positive_ev`, `best_prop_market`, `best_prop_odds`, `best_prop_ev`).
3. Add automatic client-side report polling (`AUTO_REFRESH_INTERVAL_MS = 5 * 60 * 1000`) so consensus lines and best picks stay updated while the dashboard is open.

**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_7B_FRONTEND_UI.md` — This specification.
2. `web/app.js` — Current dashboard renderer.
3. `web/styles.css` — Current design tokens & layout styles.
4. `web/tests/dashboard.test.mjs` — Current Node frontend policy tests.

### Files to Modify
- `web/app.js` (MODIFY)
- `web/styles.css` (MODIFY)
- `web/tests/dashboard.test.mjs` (MODIFY)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, `betting/*`, `api/*`, `scripts/*`, `tests/*`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Specification: `web/app.js` & `web/styles.css`

### 2.1 Bottom Navigation & Team Switcher
- Update `TAB_DEFS` in `web/app.js`:
  ```javascript
  const TAB_DEFS = [
    ["roster", "01 Roster"],
    ["adddrop", "02 Add / Drop"],
    ["totals", "03 Game Totals"],
    ["scorers", "04 Point Scorers"],
  ];
  ```
- Export `export const AUTO_REFRESH_INTERVAL_MS = 300_000;` (5 minutes).
- Helper `resolveActiveTeam(report, teamIndex = 0)`:
  - If `Array.isArray(report?.teams) && report.teams.length > 0`, returns `report.teams[Math.min(Math.max(0, teamIndex), report.teams.length - 1)]`.
  - Otherwise falls back to `{ team_key: "default", team_summary: report?.team_summary || {}, roster: report?.roster || [], recommendation: report?.recommendation || null, waiver_targets: report?.free_agents || [] }`.
- `renderHeader(report, { nowMs = Date.now(), teamIndex = 0 } = {})`:
  - Uses `resolveActiveTeam(report, teamIndex).team_summary` for `team_initials`, `team_name`, `league_name`, `Adds Left: X/Y`, and `matchup_score`.
  - When `Array.isArray(report?.teams) && report.teams.length > 1`, renders a `.team-switcher` bar with `<button class="team-switch-btn${i === teamIndex ? " active" : ""}" data-team-index="${i}">` for each team in `report.teams`.
  - Displays `active_slate_date` pill when `report?.active_slate_date` is present.

### 2.2 Connected `02 Add / Drop` Tab (`renderAddDropTab`)
- Signature: `export function renderAddDropTab(recommendation, { stale = false, waiverTargets = [], query = "" } = {})`
- Renders:
  1. The $H$-week `adddrop-card` (`RECOMMEND_ADD` / `HOLD` badge, `explanation`, `<details class="math-drawer">` with `tonight_gain`, `short_term_gain`, `asset_depreciation_penalty`, `delta_value`).
  2. Immediately below the solver card inside the same `data-panel="adddrop"` section, renders the team's connected **Top Waiver Targets** list (filtered by `query` and sorted by `ixg_per60_ewma` descending) with `<input class="fa-search" id="waiver-search" ... />` so each fantasy team's roster, add/drop recommendation, and available waiver streamers live together.

### 2.3 `04 Point Scorers` Tab (`renderPointScorersTab`)
- Signature: `export function renderPointScorersTab(pointScorers = [], { query = "", filter = "ALL" } = {})`
- Also keep `renderFreeAgentsTab` exported as an alias or helper so any existing imports continue to work.
- Renders `<section class="tab-panel" data-panel="scorers">`:
  - Search input (`id="scorer-search"`) and filter pills (`ALL`, `POINTS`, `GOALS`, `ASSISTS`, `+EV`).
  - Filters `pointScorers` by `query` (`display_name`, `team_abbr`, `opponent_abbr`, `position`) and `filter` (when `filter === "+EV"`, keeps only `p.is_positive_ev === true`; when `"GOALS"`, sorts by `prob_goal` desc; when `"ASSISTS"`, sorts by `prob_assist` desc; default sorts by `prob_point` desc).
  - For each skater row:
    - Shows `display_name`, `position · team_abbr vs opponent_abbr`, projected `G`, `A`, `Pts` (`projected_points`), `SOG`, `iXG/60`.
    - Shows model probabilities & consensus market odds:
      - `1+ Pt: ${(prob_point * 100).toFixed(0)}% (${fmtOdds(market_point_odds)})`
      - `Goal: ${(prob_goal * 100).toFixed(0)}% (${fmtOdds(market_goal_odds)})`
      - `Assist: ${(prob_assist * 100).toFixed(0)}% (${fmtOdds(market_assist_odds)})`
    - When `p.is_positive_ev === true`, renders the `+EV` badge and highlights the best profit opportunity: e.g. `${p.best_prop_market} ${fmtOdds(p.best_prop_odds)} (+${(p.best_prop_ev * 100).toFixed(1)}% EV)`.

### 2.4 `renderDashboard(report, { nowMs = Date.now(), activeTab = "roster", teamIndex = 0, query = "", scorerFilter = "ALL" } = {})`
- Resolves `activeTeam = resolveActiveTeam(report, teamIndex)`.
- Passes `activeTeam.roster` to `renderRosterTab`, `activeTeam.recommendation` and `activeTeam.waiver_targets` to `renderAddDropTab`, `report?.games` to `renderGameTotalsTab`, and `report?.point_scorers` to `renderPointScorersTab`.
- In browser glue (`if (typeof document !== "undefined")`), binds `.team-switch-btn` clicks to update `teamIndex` and re-mount, binds search/filter controls, and starts `setInterval(refreshReport, AUTO_REFRESH_INTERVAL_MS)` to keep lines and best picks automatically updated.

---

## 3. Required Policy Tests in `web/tests/dashboard.test.mjs`

Update and expand `web/tests/dashboard.test.mjs` so all tests pass:
1. `renders roster-first dashboard offline with required visual tokens and 04 Point Scorers tab`:
   - Verifies `#141619`, `#1c2026`, `#4da6ff`, `01 Roster`, `02 Add / Drop`, `03 Game Totals`, `04 Point Scorers`.
2. `switches between two Yahoo fantasy teams connecting roster, add/drop, and waiver targets`:
   - Passes a 2-team `report.teams` fixture (`teamIndex: 0` vs `teamIndex: 1`) into `renderDashboard` and asserts that switching `teamIndex` changes the header team name/adds counter, the `01 Roster` players, the `02 Add / Drop` recommendation, and the connected waiver targets.
3. `enforces 26-hour stale data gate on add/drop recommendations`.
4. `handles postponed games, zero-shot appearances, and unconfirmed goalies`.
5. `renders point scorers with Goal/Assist/Point probabilities and flags +EV profit opportunities`:
   - Verifies `renderPointScorersTab` displays `1+ Pt`, `Goal`, `Assist` probabilities and market odds, flags `+EV` skaters with `best_prop_market` and EV%, and supports `filter: "+EV"` and `filter: "GOALS"`.

---

## 4. Verification Gate
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/dashboard.test.mjs
```
Stage all modified files with `git add` and leave uncommitted for Agent 1 review.
