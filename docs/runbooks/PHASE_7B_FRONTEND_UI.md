# Runbook: Phase 7B — Premium NHL Team-Accented GUI, Two-Team Switcher, Point Scorers +EV Board & Live Auto-Refresh

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Upgrade `web/app.js`, `web/styles.css`, `web/index.html`, and `web/tests/dashboard.test.mjs` to deliver an institutional-grade, compact, high-density NHL analytics GUI featuring official NHL team SVG logos, authentic 32-team color accents, a 2-Team Fantasy switcher connecting `01 Roster` and `02 Add / Drop` (+ waiver targets), `03 Game Totals` with consensus best picks, `04 Point Scorers` with Goal/Assist/Point probabilities & `+EV` profit badges, and automatic live refresh polling.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_7B_FRONTEND_UI.md` — This specification.
2. `web/app.js` — Current dashboard renderer.
3. `web/styles.css` — Current stylesheet.
4. `web/index.html` — Current HTML shell.
5. `web/tests/dashboard.test.mjs` — Current Node policy test suite.

### Files to Modify
- `web/app.js` (MODIFY)
- `web/styles.css` (MODIFY)
- `web/index.html` (MODIFY)
- `web/tests/dashboard.test.mjs` (MODIFY)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, `betting/*`, `api/*`, `scripts/*`, `tests/*`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Visual & Interactive Specification (`web/app.js` & `web/styles.css`)

### 2.1 Authentic 32-Team NHL Color Map & Official SVG Logos
- Export `export const TEAM_COLORS` in `web/app.js` mapping all 32 NHL abbreviations (`EDM`, `COL`, `TOR`, `CAR`, `FLA`, `NYR`, `BOS`, `TBL`, `DAL`, `VGK`, `WPG`, `VAN`, `CGY`, `LAK`, `SEA`, `NJD`, `WSH`, `PIT`, `PHI`, `CBJ`, `DET`, `BUF`, `OTT`, `MTL`, `STL`, `MIN`, `NSH`, `CHI`, `UTA`, `ANA`, `SJS`, `NYI`) to `[primaryHex, secondaryHex]`, e.g.:
  - `EDM: ["#FF4C00", "#041E42"]`, `COL: ["#6F263D", "#236192"]`, `TOR: ["#00205B", "#4da6ff"]`, `CAR: ["#CC0000", "#111111"]`, `FLA: ["#C8102E", "#B9975B"]`, `NYR: ["#0038A8", "#CE1126"]`, `VGK: ["#B4975A", "#333F42"]`, `BOS: ["#FFB81C", "#111111"]`, `DAL: ["#006847", "#8F8F8C"]`, `VAN: ["#00843D", "#00205B"]`, `WPG: ["#041E42", "#AC162C"]`, `TBL: ["#002868", "#4da6ff"]`, etc., with fallback `["#4da6ff", "#1c2026"]`.
- Helper `export function renderTeamLogo(abbr)`:
  - When `abbr` is non-empty, returns a compact team crest wrapper containing both the official NHL dark SVG logo and a styled fallback monogram badge:
    ```html
    <span class="team-crest" style="--team-color:${primary};--team-secondary:${secondary}">
      <img class="team-logo" src="https://assets.nhle.com/logos/nhl/svg/${esc(upper)}_dark.svg" alt="${esc(upper)}" loading="lazy" onerror="this.style.display='none'" />
      <span class="team-Initials">${esc(upper)}</span>
    </span>
    ```
- Every player card/row (`roster-row`, `fa-row`, `scorer-row`) sets inline style `--team-color: ${primary}` so CSS renders a crisp 3px left accent bar and subtle radial team-color glow on hover/active!
- Every matchup card (`game-card`) sets `--away-color` and `--home-color` for a dual-team gradient top accent bar and split $\lambda_{\text{away}}$ vs $\lambda_{\text{home}}$ tug-of-war bar.

### 2.2 Sticky Glassmorphic Header & 2-Team Fantasy Switcher (`renderHeader`)
- Preserves the core SRS §6.1.2 dark tokens in `web/styles.css` (`#141619` base, `#1c2026` surface, `#4da6ff` frost cyan) enhanced with glassmorphic backdrop blur (`backdrop-filter: blur(14px)`), crisp 1px borders (`rgba(255,255,255,0.07)`), and `font-variant-numeric: tabular-nums`.
- `export function resolveActiveTeam(report, teamIndex = 0)`:
  - Returns `report.teams[teamIndex]` when `Array.isArray(report?.teams) && report.teams.length > 0` (clamped to valid index), else falls back to top-level `{ team_key: "team_1", team_summary: report?.team_summary || {}, roster: report?.roster || [], recommendation: report?.recommendation || null, waiver_targets: report?.free_agents || [] }`.
- `export function renderHeader(report, { nowMs = Date.now(), teamIndex = 0 } = {})`:
  - Renders active team avatar, `team_name`, `league_name`, `Adds Left: <strong>${addsLeft}/${fmtInt(limit)}</strong>`, `matchup_score` pill, `active_slate_date` pill (`Slate: ${report.active_slate_date}` when present), and a live pulse pill (`LIVE · 5m Sync`).
  - When `Array.isArray(report?.teams) && report.teams.length > 1`, renders a segmented `.team-switcher` bar with `<button class="team-switch-btn${i === teamIndex ? " active" : ""}" data-team-index="${i}">` showing each fantasy team's name & league so clicking switches both `01 Roster` and `02 Add / Drop` (+ waiver targets).
  - Preserves the 26-hour `STALE DATA` banner when `isReportStale` is true.

### 2.3 Four Connected Tabs
Update `TAB_DEFS`:
```javascript
const TAB_DEFS = [
  ["roster", "01 Roster"],
  ["adddrop", "02 Add / Drop"],
  ["totals", "03 Game Totals"],
  ["scorers", "04 Point Scorers"],
];
```
1. **`01 Roster` (`renderRosterTab(roster = [])`):**
   - Renders each rostered player in slot order (`C, LW, RW, D, UTIL, G, BN, IR, IR+`) with `.slot-chip`, `renderTeamLogo(p.team_abbr)`, player name + goalie status badge (`CONFIRMED` gold / `PROJECTED` steel), matchup subtitle (`EDM vs COL`), opponent defense pill (`DefRtg` when `opp_def_rtg_rel` is non-zero), compact stat pills (`SOG <b>0</b>`, `iXG <b>0.00</b>`, `G <b>0</b>`, `A <b>0</b>`, `HIT`, `BLK`), and a glowing projected FP badge (`${fmt1(p.projected_fp)} <small>proj FP</small>`).
   - Never emits `NaN`, `null`, or `undefined` on zero-stat players (`>0<` and `0.0` preserved for existing policy assertions).
2. **`02 Add / Drop` (`renderAddDropTab(recommendation, { stale = false, waiverTargets = [], query = "" } = {})`):**
   - Top section: Streaming Solver card (`<article class="adddrop-card...">`) with `RECOMMEND_ADD` / `HOLD` badge, `explanation`, visual comparison bar of Streamer vs Drop weekly + ROS value, and `<details class="math-drawer">` (`Tonight gain`, `Weekly short-term gain`, `Asset depreciation`, `Net delta`). Honors `stale === true` (`stale-disabled`, `data-stale="true"`, `STALE DATA`).
   - Connected Bottom section inside the same panel: **Available Waiver Streamers for This Team** (`waiverTargets`), with search input (`id="waiver-search"`) and compact player cards ranked by `ixg_per60_ewma` descending with mini process-metric bars (`iXG/60`, `iCorsi/60`, `PP1`, `proj FP`).
   - Also keep `export function renderFreeAgentsTab(freeAgents = [], { query = "" } = {})` exported so any existing callers/tests importing `renderFreeAgentsTab` directly continue to pass!
3. **`03 Game Totals` (`renderGameTotalsTab(games = [])`):**
   - Sorts games chronologically by `start_time_utc`.
   - Renders `<article class="game-card">` for each game (keep exact `<article class="game-card">` opening tag so `totalsHtml.split('<article class="game-card">')` in tests works cleanly!).
   - Displays both teams' logos (`renderTeamLogo(g.away_abbr)` @ `renderTeamLogo(g.home_abbr)`), game start time + status badge, a visual $\lambda_{\text{away}}$ vs $\lambda_{\text{home}}$ expected-goals split bar, `Model <b>${fmt1(g.expected_total)}</b>` vs `Line <b>${fmt1(g.line)}</b>`, `P(Over) <b>${fmt2(g.prob_over)}</b>`, Over/Under American odds (`O ${fmtOdds(g.over_odds)} / U ${fmtOdds(g.under_odds)}`), goalie confirmation badges (`CONFIRMED` / `PROJECTED`), and when `g.is_positive_ev === true && !postponed`, renders the `+EV` badge plus a recommended pick callout (`BEST PICK: ${g.best_side || "OVER"} ${fmt1(g.line)} (${fmtSignedPct(g.ev)} EV)`).
   - When `status === "POSTPONED"`, renders `POSTPONED` badge and suppresses `+EV` tags.
4. **`04 Point Scorers` (`renderPointScorersTab(pointScorers = [], { query = "", filter = "ALL" } = {})`):**
   - Renders `<section class="tab-panel" data-panel="scorers">` with search input (`id="scorer-search"`) and filter chips (`ALL`, `POINTS`, `GOALS`, `ASSISTS`, `+EV`).
   - Filters by `query` and `filter`:
     - `"ALL"` or `"POINTS"`: sort by `prob_point` desc (tie-break `best_prop_ev` desc)
     - `"GOALS"`: sort by `prob_goal` desc
     - `"ASSISTS"`: sort by `prob_assist` desc
     - `"+EV"`: filter to `p.is_positive_ev === true`, sort by `best_prop_ev` desc
   - Each scorer card (`<div class="scorer-row" style="--team-color:...">`) shows:
     - `renderTeamLogo(p.team_abbr)`, `display_name`, `position · team_abbr vs opponent_abbr`, projected `G`, `A`, `Pts` (`projected_points`), `SOG`, `iXG/60`.
     - Three compact probability + consensus market odds pills with mini probability fill bars:
       - `1+ Pt: <b>${Math.round(Number(p.prob_point || 0) * 100)}%</b> (${fmtOdds(p.market_point_odds)})`
       - `Goal: <b>${Math.round(Number(p.prob_goal || 0) * 100)}%</b> (${fmtOdds(p.market_goal_odds)})`
       - `Assist: <b>${Math.round(Number(p.prob_assist || 0) * 100)}%</b> (${fmtOdds(p.market_assist_odds)})`
     - When `p.is_positive_ev === true`, renders the `+EV` badge and a profit opportunity banner: `PROFIT EDGE: ${esc(p.best_prop_market)} ${fmtOdds(p.best_prop_odds)} (${fmtSignedPct(p.best_prop_ev)} EV)`.

### 2.4 Auto-Refresh Polling (`AUTO_REFRESH_INTERVAL_MS`)
- Export `export const AUTO_REFRESH_INTERVAL_MS = 300_000;` (5 minutes).
- In the browser glue (`if (typeof document !== "undefined")`), bind `.team-switch-btn`, `.nav-btn`, `#waiver-search`, `#scorer-search`, and `.scorer-filter-btn`, and set up `setInterval` every `AUTO_REFRESH_INTERVAL_MS` (plus a manual "Sync Now" button in the header) that re-fetches `./report.json` with `{ cache: "no-store" }` and re-mounts while preserving the user's active tab, selected team index, and search/filter state!

---

## 3. Required Policy Tests in `web/tests/dashboard.test.mjs`

Ensure all 6 tests in `web/tests/dashboard.test.mjs` pass:
1. `renders roster-first dashboard offline with required visual tokens, team logos, and 04 Point Scorers tab`
2. `switches between two Yahoo fantasy teams connecting roster, add/drop, and waiver targets`
3. `enforces 26-hour stale data gate on add/drop recommendations`
4. `handles postponed games, zero-shot appearances, and unconfirmed goalies`
5. `ranks and filters free agents / waiver targets by 15-game EWMA process metrics`
6. `renders point scorers with Goal/Assist/Point probabilities and flags +EV profit opportunities`

---

## 4. Verification Gate
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
node --test web/tests/dashboard.test.mjs
```
Stage all modified files with `git add` and leave uncommitted for Agent 1 review.
