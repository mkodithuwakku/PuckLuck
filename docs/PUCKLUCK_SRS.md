# Software Requirements Specification (SRS)
## Project Codename: PuckLuck (NHL Predictive Analytics, Fantasy Optimization & Market Pricing System)

**Document Version:** 1.0.0  
**Status:** Approved for Implementation  
**Target Architecture:** Python 3.11+ / SQLite / Parquet / FastAPI / Modern Vanilla JS  
**Operational Target:** Local macOS Daemon (`launchd`) with Zero-Cost Static Phone Interface (GitHub Pages) + OAuth Ingestion  

---

## 1. Executive Summary & Problem Definition

### 1.1 Product Vision
PuckLuck is an institutional-grade, personal sports analytics platform that unifies **NHL fantasy roster optimization (Yahoo Fantasy)** with **probabilistic betting market pricing (Game Totals and Player Points)**. 

### 1.2 Core Operational Challenges
1. **The Irreversible Transaction Dilemma (Waiver Dropping):**  
   In competitive Yahoo Fantasy Hockey leagues, streaming a skater for an off-night matchup permanently forfeits the dropped player to waivers. Conventional tools greedily optimize for today's points, sacrificing multi-week asset value. PuckLuck models multi-week net replacement cost ($H$-week asset depreciation) to enforce mathematical `HOLD` constraints.
2. **Noise vs. Signal in Hockey Analytics:**  
   Unlike basketball or baseball, hockey scoring is a high-variance, low-sample Poisson event. Unregressed counting stats (Goals, Assists) are dominated by shooting percentage variance (puck luck). PuckLuck derives predictive features exclusively from high-signal underlying process metrics: Expected Goals (xG), Shot Attempts (Corsi/Fenwick), High-Danger Scoring Chances (HDCF), Power Play Deployment (PP1 TOI share), and Opponent Goals Saved Above Expected (GSAx).
3. **Dual-Market Unified Engine:**  
   The identical feature store driving player fantasy point distributions also determines team-level expected goal intensities ($\lambda_{\text{home}}, \lambda_{\text{away}}$), enabling simultaneous computation of fantasy add/drop recommendations and positive-expectation ($+\text{EV}$) game total betting projections.

---

## 2. System Architecture & Operational Boundaries

```
┌────────────────────────────────────────────────────────────────────────┐
│                        INGESTION LAYER (05:30 ET)                      │
│  ├─ Yahoo Fantasy API (OAuth 2.0: Rosters, Rules, Free Agents, FAAB)   │
│  ├─ Official NHL API (Free REST: Rosters, Lines, Skater/Goalie Box)    │
│  └─ MoneyPuck Bulk Engine (Static Parquet/CSV: xG, HDCF, On-Ice Stats) │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
┌────────────────────────────────────▼───────────────────────────────────┐
│               LOCAL DATA WAREHOUSE & CROSSWALK (SQLite)                │
│  ├─ dim_players (Crosswalk: yahoo_id <-> nhl_id <-> moneypuck_id)      │
│  ├─ dim_teams (Pace, Rest, Venue, Altitude, Travel Fatigue)            │
│  ├─ fact_skater_game_logs (5v5 & All-Situations xG, SOG, Hits, Blocks) │
│  └─ fact_goalie_game_logs (GSAx, HD Save %, Freeze Frequency)          │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
┌────────────────────────────────────▼───────────────────────────────────┐
│                    FEATURE STORE & STATISTICAL ENGINE                  │
│  ├─ 15-Game EWMA Process Metrics (xG/60, Shots/60, Individual HDCF)   │
│  ├─ Goalie Matchup Adjustments (Opponent GSAx/60 Suppression)          │
│  └─ Game Pace & Possession Factoring (Team 5v5 Possessions)            │
└───────────────────────┬────────────────────────┬───────────────────────┘
                        │                        │
┌───────────────────────▼────────┐      ┌────────▼───────────────────────┐
│  SUB-SYSTEM A: FANTASY ENGINE  │      │ SUB-SYSTEM B: BETTING ENGINE   │
│  ├─ Component Regressions      │      │ ├─ Bivariate Poisson Simulator │
│  ├─ League Scoring Rescorer    │      │ ├─ Over/Under Probabilities    │
│  └─ H-Week Asset Penalty Solver│      │ └─ Consensus +EV Price Assessor│
└───────────────────────┬────────┘      └────────┬───────────────────────┘
                        │                        │
┌───────────────────────▼────────────────────────▼───────────────────────┐
│                       DELIVERY & ORCHESTRATION                         │
│  ├─ Local macOS LaunchAgent (`com.puckluck.refresh`)                   │
│  ├─ Local Interactive GUI / API (FastAPI / Loopback: 127.0.0.1:8877)   │
│  └─ Static Distribution (Allowlisted JSON -> GitHub Pages Dashboard)  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data Ingestion & Normalization Specification

### 3.1 External Data Contracts

#### Source 1: Yahoo Fantasy Sports API (`OAuth 2.0`)
- **Protocol:** REST with Bearer Token (`https://fantasysports.yahooapis.com/fantasy/v2/`).
- **Authentication Flow:**
  - Registered Yahoo Developer App (`Client ID`, `Client Secret`).
  - Out-of-band authorization code grant on initialization.
  - Automatic background token renewal using persisted `refresh_token` in local `.env`.
- **Payloads Extracted:**
  - `league/{league_key}/settings`: Stat categories and point weights (G, A, +/-, PIM, PPP, SHP, SOG, FW, HIT, BLK; W, GA, SV, SHO).
  - `team/{team_key}/roster`: Active lineup slots (`C`, `LW`, `RW`, `D`, `UTIL`, `G`, `BN`, `IR`, `IR+`).
  - `league/{league_key}/players;status=FA`: Free agent waiver pool with current ownership.

#### Source 2: Official NHL API (`api-web.nhle.com/v1/`)
- **Protocol:** Public unauthenticated REST (`GET`).
- **Key Endpoints:**
  - `/schedule/{date}`: Daily slate, start times, broadcast networks.
  - `/roster/{team}/current`: Active 23-man rosters and positions.
  - `/gamecenter/{game_id}/boxscore`: Official box scores, ice time, TOI per situation (EV, PP, SH).

#### Source 3: MoneyPuck Bulk Engine
- **Protocol:** Daily static CSV bulk ingest via HTTP GET with custom `User-Agent`.
- **Target URL:** `https://moneypuck.com/moneypuck/playerData/seasonSummary/{season}/regular/skaters.csv`.
- **Target Metrics:**
  - Skaters: `I_F_xGoals` (Individual xG), `I_F_shotAttempts` (Individual Corsi), `I_F_highDangerShots`, `OnIce_F_xGoals`, `icetime`.
  - Goalies: `goalsSavedAboveExpected` (GSAx), `savePercentage`, `highDangerShotsAgainst`.

### 3.2 Relational SQLite Schema (`data/puckluck.sqlite3`)

The database MUST enforce relational integrity via foreign keys (`PRAGMA foreign_keys = ON;`).

```sql
-- 1. Identity Crosswalk
CREATE TABLE IF NOT EXISTS dim_players (
    player_id INTEGER PRIMARY KEY,          -- Official NHL.com Player ID
    yahoo_player_id TEXT UNIQUE,            -- Yahoo Fantasy Player Key
    canonical_name TEXT NOT NULL,           -- Lowercase normalized (e.g. "connor mcdavid")
    display_name TEXT NOT NULL,             -- Formatted ("Connor McDavid")
    primary_position TEXT NOT NULL,         -- C, LW, RW, D, G
    shoots_catches TEXT,                    -- L, R
    birth_date TEXT                         -- YYYY-MM-DD
);
CREATE INDEX IF NOT EXISTS idx_players_canonical ON dim_players(canonical_name);

-- 2. Team Dimension
CREATE TABLE IF NOT EXISTS dim_teams (
    team_id INTEGER PRIMARY KEY,            -- NHL API Team ID
    abbreviation TEXT NOT NULL UNIQUE,      -- EDM, FLA, NYR, etc.
    team_name TEXT NOT NULL,
    venue TEXT NOT NULL,
    altitude_ft INTEGER DEFAULT 0           -- Altitude for fatigue modeling (e.g. DEN = 5280)
);

-- 3. Fact: Skater Box Scores with Advanced Process Metrics
CREATE TABLE IF NOT EXISTS fact_skater_games (
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL REFERENCES dim_players(player_id),
    season INTEGER NOT NULL,
    game_date TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    opponent_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    is_home INTEGER NOT NULL,
    rest_days INTEGER NOT NULL,             -- 0 = Back-to-Back, 1 = 1 day, >=2
    toi_seconds INTEGER NOT NULL,
    pp_toi_seconds INTEGER NOT NULL,
    goals INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    shots_on_goal INTEGER NOT NULL DEFAULT 0,
    blocks INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    faceoffs_won INTEGER NOT NULL DEFAULT 0,
    pim INTEGER NOT NULL DEFAULT 0,
    -- Advanced MoneyPuck Metrics:
    ixg REAL NOT NULL DEFAULT 0.0,          -- Individual Expected Goals
    icorsi INTEGER NOT NULL DEFAULT 0,      -- Individual Shot Attempts
    ihdcf INTEGER NOT NULL DEFAULT 0,       -- Individual High Danger Chances
    on_ice_xg_share REAL DEFAULT 0.0,       -- On-ice xGF% at 5v5
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_skaters_date ON fact_skater_games(game_date);
CREATE INDEX IF NOT EXISTS idx_skaters_lookup ON fact_skater_games(player_id, game_date);

-- 4. Fact: Goalie Performances & Defense Suppression
CREATE TABLE IF NOT EXISTS fact_goalie_games (
    game_id INTEGER NOT NULL,
    goalie_id INTEGER NOT NULL REFERENCES dim_players(player_id),
    season INTEGER NOT NULL,
    game_date TEXT NOT NULL,
    team_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    opponent_id INTEGER NOT NULL REFERENCES dim_teams(team_id),
    is_home INTEGER NOT NULL,
    decision TEXT,                          -- W, L, OTL
    toi_seconds INTEGER NOT NULL,
    shots_against INTEGER NOT NULL,
    goals_against INTEGER NOT NULL,
    saves INTEGER NOT NULL,
    gsax REAL NOT NULL,                     -- Goals Saved Above Expected
    hd_save_pct REAL,                       -- High Danger Save Percentage
    PRIMARY KEY (game_id, goalie_id)
);

-- 5. Stored Betting & Market Feeds
CREATE TABLE IF NOT EXISTS fact_market_lines (
    game_id INTEGER NOT NULL,
    sportsbook TEXT NOT NULL,
    market_type TEXT NOT NULL,              -- TOTAL_GOALS, SPREAD, PUCK_LINE, PLAYER_POINTS
    target_id TEXT,                         -- NULL for game total, player_id for props
    line REAL NOT NULL,                     -- e.g. 6.5 goals, 1.5 points
    over_odds INTEGER NOT NULL,             -- American odds (-110)
    under_odds INTEGER NOT NULL,            -- American odds (-110)
    captured_at TEXT NOT NULL,
    PRIMARY KEY (game_id, sportsbook, market_type, target_id)
);
```

---

## 4. Sub-System A: Fantasy Projection & Add/Drop Solver

### 4.1 Component-Level Prediction Architecture
The model MUST NOT predict aggregate fantasy points directly. Fantasy points are an artifact of idiosyncratic league weights. The model predicts the **underlying statistical components** ($s \in \{\text{SOG, iXG, G, A, BLK, HIT}\}$), then maps them through the target league's scoring formula.

#### Feature Vector Formulation ($X_{p, g}$ for Player $p$ in Target Game $g$):
1. **Exponentially Weighted Moving Averages (EWMA, $\alpha = 0.08 \approx 15 \text{ games}$):**
   - Individual Expected Goals per 60 minutes: $\overline{\text{iXG}}_{60}$
   - Individual Shot Attempts per 60 minutes: $\overline{\text{iCorsi}}_{60}$
   - Individual High Danger Chances per 60 minutes: $\overline{\text{iHDCF}}_{60}$
   - Power Play Time on Ice Share: $\text{TOI}_{\text{PP}} / \text{Team TOI}_{\text{PP}}$
2. **Opponent Defensive Metric:**
   - Opponent 5v5 xGA/60 relative to league average ($\text{DefRtg}_{\text{rel}}$).
   - Confirmed Opponent Starting Goalie Rolling $\text{GSAx}/60$.
3. **Schedule Context:**
   - Rest differential ($\Delta \text{Rest} = \text{Rest}_{\text{Team}} - \text{Rest}_{\text{Opp}}$).
   - $\text{B2B}_{\text{flag}} \in \{0, 1\}$.
   - Home ice advantage ($\text{Home}_{\text{flag}} \in \{0, 1\}$).

#### Component Regressions (Closed-Form Ridge Regularization):
$$\beta_s = (X^T X + \lambda I)^{-1} X^T y_s$$
Predictions are clamped to nonnegative bounds: $\hat{s} = \max(0, X \beta_s)$.

#### Scoring Rescorer:
$$\widehat{\text{FP}}_{p, g} = \sum_{s} \left(\hat{s} \times \text{Weight}_{s}\right) + \text{BonusAdjustments}(\hat{s})$$

### 4.2 The Irreversible Transaction Solver (Streaming Add/Drop Gate)
Let candidate streamer $A$ be on free agency, and candidate drop $D$ be on the active roster.
Let $T_{\text{week}}$ be the remaining days in the active Yahoo fantasy week.
Let $H$ be the retention horizon parameter (default $H = 2.5 \text{ weeks}$).

#### Mathematical Condition for an Add/Drop:
An add/drop recommendation is emitted **if and only if** the Net Expected Value $\Delta \text{Value}$ is strictly positive:

$$\Delta \text{Value} = \underbrace{\left(\widehat{\text{FP}}_{A, \text{Tonight}} + \sum_{t=1}^{T_{\text{week}}} \widehat{\text{FP}}_{A, t}\right)}_{\text{Streamer Total Weekly Points}} - \underbrace{\left(\sum_{t=1}^{T_{\text{week}}} \widehat{\text{FP}}_{D, t}\right)}_{\text{Drop Total Weekly Points}} + \underbrace{H \times \left( \overline{\text{FP}}_{A, \text{ROS}} - \overline{\text{FP}}_{D, \text{ROS}} \right)}_{\text{Long-Term Asset Depreciation Penalty}}$$

#### Decision Rules:
- If $\Delta \text{Value} > 0$: Issue `RECOMMEND_SWAP` with explicit net gain estimate.
- If $\Delta \text{Value} \le 0$: Issue `HOLD_ASSET`. The interface MUST explicitly display:
  *"Short-term streaming gain of $+X.X$ FP tonight is rejected: dropping $D$ results in net loss of $-Y.Y$ FP over the next $H$ weeks."*

---

## 5. Sub-System B: Betting Engine (Total Goal Lines)

### 5.1 Bivariate Poisson Goal Model
Hockey game scoring is modeled as two coupled Poisson processes for Home ($H$) and Away ($A$) goals.

$$\lambda_H = \text{BaseRate} \times \text{TeamOffense}_H \times \text{OpponentDefense}_A \times \text{VenueHome} \times \text{RestFactor}_H \times \text{GoalieAdjustment}_A$$
$$\lambda_A = \text{BaseRate} \times \text{TeamOffense}_A \times \text{OpponentDefense}_H \times \text{RestFactor}_A \times \text{GoalieAdjustment}_H$$

Where:
- $\text{GoalieAdjustment} = 1.0 - \left( \frac{\text{GSAx}/60}{3.0} \right)$
- Backup goalie unconfirmed penalty: $+0.22 \text{ goals to } \lambda$.

### 5.2 Pricing the Total Goal Line (e.g. Over/Under 6.5)
The probability mass function of total goals $K = H + A$ is evaluated across $H \in [0, 15], A \in [0, 15]$:

$$P(\text{Over } L) = \sum_{h=0}^{15} \sum_{a=0}^{15} \mathbb{I}(h + a > L) \cdot \frac{\lambda_H^h e^{-\lambda_H}}{h!} \cdot \frac{\lambda_A^a e^{-\lambda_A}}{a!}$$

### 5.3 Expected Value ($+\text{EV}$) Gate
Let decimal sportsbook odds be $O_{\text{book}}$ and model probability be $P_{\text{model}}$:

$$\text{EV} = (P_{\text{model}} \times O_{\text{book}}) - 1.0$$

The engine flags market opportunities only when $\text{EV} \ge +0.035$ ($+3.5\%$ edge), filtering out low-liquidity books.

---

## 6. Mobile Dashboard & Static Cloud Distribution

PuckLuck provides a high-density, phone-optimized daily scouting dashboard that operates with **zero hosting costs, zero server maintenance, and no exposed ports**.

```
[Local Mac Pipeline (06:00 ET)]
       │
       ├─ 1. Ingests Yahoo, NHL API & MoneyPuck
       ├─ 2. Computes Add/Drop Solver & Goal Line Predictions
       ├─ 3. Strips secrets & exports public dist/report.json + web/ assets
       └─ 4. Git pushes to `gh-pages` branch on GitHub
                 │
                 ▼
[GitHub Pages CDN (Global HTTPS)]
                 │
                 ▼
[Phone / Desktop Browser] (https://<username>.github.io/puckluck/)
       ├─ Mobile-first Roster Sheet (Roster on top, no massive headers)
       ├─ Swipeable Team / League Selector
       ├─ Daily Matchup Cards with Over/Under Goal Totals & EV tags
       └─ Expandable Add/Drop Recommendation Drawer with Multi-Week Net Math
```

### 6.1 Mobile UI/UX Design System
In alignment with the institutional design standards of the repository:
1. **Roster-First Hierarchy (Mobile & Desktop):**
   - The user’s active Yahoo roster sheet is the **primary view above the fold**.
   - Compact header: team avatar/initials, transaction counter (e.g. `Adds Left: 3/4`), and matchup score.
   - Secondary panels (betting boards, league standings, waiver wire explorer) sit below or in dedicated navigation tabs.
2. **Palette & Visual Tokens:**
   - **Base:** Deep charcoal (`#141619`) with ice-steel undertones (`#1c2026`).
   - **Accents:** Muted frost cyan (`#4da6ff`) for primary interactive controls, cold steel for secondary text.
   - **Status Badges:** Subtle gold for confirmed starting goalies (`CONFIRMED`), muted steel for unconfirmed (`PROJECTED`), emerald green for `RECOMMEND_ADD`, and amber/plum for `HOLD`.
3. **Responsive Breakpoints:**
   - Designed for standard mobile viewports ($375\text{px} - 430\text{px}$) with fixed bottom navigation:
     - `01 Roster`: Active roster, tonight's starting slots, opponent defensive ranking, and individual projected points.
     - `02 Add / Drop`: The streaming solver card, displaying top free-agent streamer, weakest roster drop, tonight's gain, and multi-week net penalty.
     - `03 Game Totals`: Nightly NHL slate sorted by time, displaying model projected total vs sportsbook line and $+\text{EV}$ edge flags.
     - `04 Free Agents`: Searchable waiver pool ranked by 15-game EWMA process metrics ($\text{iXG}_{60}, \text{iCorsi}_{60}, \text{PP1 Share}$).
4. **PWA Capabilities:**
   - Manifest and service worker enabled for iOS / Android "Add to Home Screen" support (fullscreen standalone mode, app icon, no URL bar).

### 6.2 The Static Security & Privacy Boundary
1. **Allowlisted Static Export (`dist/report.json`):**
   - The static JSON export contains ONLY display-safe fields: player names, positions, teams, opponents, projected fantasy points, recommended swaps, game totals, and betting edges.
   - **Hard Exclusion:** Yahoo OAuth tokens, developer secrets, internal file paths, and private account credentials NEVER enter the static directory or GitHub repository.
2. **Client-Side Cache & Stale Detection:**
   - Static client checks `report.json`'s `generated_at` timestamp.
   - If the report is older than 26 hours, actionable add/drop recommendations are grayed out with a `STALE DATA` warning.

---

## 7. Security, Local Boundaries & Deployment

### 7.1 Security Boundary
1. **OAuth Secrets:** Yahoo `Client Secret` and `refresh_token` MUST remain inside an ignored `.env` file on the local machine. They are NEVER included in static JSON exports or public git commits.
2. **Loopback Server:** The local FastAPI backend binds strictly to `127.0.0.1:8877`. Cross-origin resource sharing (CORS) is restricted to local development origins.

### 7.2 Automation Daemon (`launchd`)
A macOS LaunchAgent (`com.puckluck.refresh.plist`) executes daily at 06:00 America/Edmonton:
1. Refreshes Yahoo OAuth tokens and pulls active rosters.
2. Downloads MoneyPuck bulk CSVs and ingests box scores into `fact_skater_games`.
3. Fits component Ridge regressions on games strictly before today ($t < T_{\text{today}}$).
4. Solves daily add/drop recommendations and bivariate Poisson game totals.
5. Generates `dist/report.json` containing only allowlisted public fields.
6. Pushes static export to GitHub Pages branch (`gh-pages`) for immediate mobile access.

---

## 8. Verification & Quality Gates

Every code change must pass three automated test suites before being merged:

1. **Python Unit & Regression Suite (`pytest`):**
   - Socket blocking: Unit tests MUST prohibit network access (`conftest.py` disables `socket.connect`).
   - Temporal validation: Confirm modifying tomorrow's game stats cannot leak into today's feature matrices.
   - Mathematical invariance: Verify closed-form Ridge solver matches scikit-learn output within $1e-5$.
   - Transaction solver: Verify drop penalty correctly forces `HOLD` on elite assets.
2. **Deterministic Code Formatter (`black`):**
   - 100% compliance with `black *.py tests/`.
3. **Frontend Policy Test Suite (`node --test`):**
   - Verify static client renders correctly without network connectivity.
   - Verify edge-case rendering (postponed games, zero-shot appearances, unconfirmed goalies).

---

## 8. Development Protocol: Two-Agent Dual-Engine Pattern & Token Minimization

To achieve enterprise software quality while preventing agent context thrashing and excessive token expenditure, PuckLuck development strictly follows the **Two-Agent Dual-Engine Pattern**:

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             AGENT 1: DOCUMENTATION & PROMPT AGENT                │
│  • Operates on architecture, data models, edge cases, and verification runbooks  │
│  • Audits repository diffs, maintains living docs, and author atomic runbooks    │
│  • Engineers bounded, context-constrained implementation prompts for Agent 2     │
└────────────────────────────────────────┬─────────────────────────────────────────┘
                                         │
                                         │ Hands off single atomic runbook prompt
                                         ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                              AGENT 2: CODING / IMPLEMENTATION AGENT              │
│  • Implements ONE isolated module/phase per session (never open-ended refactors) │
│  • Reads only the 2-3 files specified in the runbook (saves 80%+ exploratory tokens) │
│  • Writes deterministic tests, runs pytest/black/node verification               │
│  • Leaves changes staged for Agent 1 review and verification                     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 8.1 The Token Expenditure Bottleneck & Mitigation
- **The Failure Mode:** When coding agents are prompted with broad directives (*"implement the fantasy add/drop solver"*), they spend 30,000–60,000 tokens crawling directories, parsing large files, and misinterpreting data structures.
- **The Bounded Context Protocol:**
  1. Agent 1 authors an isolated **Implementation Runbook** (`docs/runbooks/<FEATURE>.md`) specifying:
     - Exact schema and mathematical equations.
     - Files to touch vs. files strictly protected from modification.
     - Concrete function signatures and type contracts.
     - Deterministic test cases with synthetic test fixtures.
  2. The manager copies Agent 1's generated prompt to Agent 2.
  3. Agent 2 executes the task with minimal context churn, running unit tests until green.
  4. Agent 1 reviews the diff, records benchmark numbers, and commits/pushes to the development branch.

### 8.2 Initial Agent Documentation Bootstrap
Before writing any application code in the new repository, Agent 1 MUST initialize the standard agent context documentation:

1. **Root `AGENTS.md` (Always-On System Rule):**
   - High-density project summary, virtualenv path (`.venv/bin/python`), and verification commands (`pytest`, `black`, `node --test`).
   - **File Routing Index:** A 1-line lookup table mapping every feature to its single responsible module (e.g. `ingestion/moneypuck.py`, `features/builder.py`, `models/ridge.py`).
   - Invariant constraints: offline-only tests, socket blocking, no credentials in git.
2. **`docs/agents/ARCHITECTURE_MAP.md`:**
   - Module responsibility table, complete SQLite table schemas, and data flow diagrams.
3. **`docs/agents/WORKFLOWS_AND_TESTING.md`:**
   - Verification commands by subsystem, mock conventions, LaunchAgent installation guides, and operational runbook links.

---

## 10. Implementation Backlog & Phasing

- [x] **Phase 0: Repository Setup & Agent Documentation Bootstrap**
  - Initialize git repository, `.gitignore`, `.env.example`, and Python virtualenv (`requirements.txt`).
  - Create root `AGENTS.md`, `docs/agents/ARCHITECTURE_MAP.md`, and `docs/agents/WORKFLOWS_AND_TESTING.md`.
  - Configure `conftest.py` with offline socket blocking.
- [x] **Phase 1: Database & Crosswalk Infrastructure**
  - Implement SQLite v2 schema in `db.py`.
  - Build NHL-to-Yahoo player crosswalk resolver.
  - Implement MoneyPuck automated CSV ingestion pipeline.
- [x] **Phase 2: Yahoo Fantasy Integration**
  - Implement headless OAuth 2.0 handshake and token persistence.
  - Ingest user rosters, active lineup slots, and league scoring categories.
- [x] **Phase 3: Statistical Modeling & Feature Pipeline**
  - Build `FeatureBuilder` for team pace, possessions, and 15-game EWMA xG.
  - Implement closed-form `ComponentRidge` predictor.
  - Implement the $H$-week asset penalty add/drop solver.
- [x] **Phase 4: Game Totals & Betting Evaluator**
  - Implement Bivariate Poisson simulator.
  - Build consensus line odds parser and $+\text{EV}$ screener.
- [x] **Phase 5: Automated Scheduler & Static Dashboard**
  - Build LaunchAgent plist and publication pipeline.
  - Implement responsive desktop/mobile UI in `web/`.
- [x] **Phase 6: End-to-End Warehouse Orchestration, Yahoo-Optional Mode & Live Server Mount**
  - Wire `scripts/refresh.py` to execute the full warehouse-to-report pipeline (`FeatureBuilder` → `ComponentRidgeModel` → `ScoringRescorer` → `solve_best_stream` + `price_total_goals` → `dist/report.json`) with Yahoo-optional fallback (`DEFAULT_STAT_WEIGHTS` + `--demo` warehouse seeder).
  - Mount `web/` static assets and `/report.json` on `api/server.py` so `http://127.0.0.1:8877/` serves the live interactive PWA out of the box.
- [x] **Phase 7A: Live NHL Slate Rollover, Consensus Lines, Two-Team Fantasy & Player Prop +EV Backend**
  - Wire `scripts/refresh.py --live` with late-night rollover (`resolve_active_slate`), open-source consensus lines (`fetch_open_consensus_lines`), 2-team fantasy bundles (`report.teams`), and Point/Goal/Assist scorer Poisson probabilities + consensus prop `+EV` screening (`report.point_scorers`).
- [x] **Phase 7B: Two-Team Connected UI, Point Scorers Tab & Live Auto-Refresh Polling (`web/`)**
  - Add 2-team switcher in header connecting `01 Roster` and `02 Add / Drop` (with team-specific waiver targets), replace `04 Free Agents` with `04 Point Scorers` (Goals, Assists, Points + `+EV` prop flags), and add automatic client-side report polling.
- [x] **Phase 8: Bi-Weekly Walk-Forward Backtesting, Self-Tuning Engine & Sleep-Proof Cloud Automation**
  - Implement `models/backtest.py` to evaluate out-of-sample component MAE/RMSE, Bivariate Poisson Brier/log-loss, and `+EV` pick ROI every 2 weeks, grid-searching $\alpha$ (EWMA), $\lambda$ (Ridge L2), and Poisson base rates to emit actionable model upgrade recommendations and persist `data/model_params.json`.
  - Add **sleep-proof GitHub Actions scheduled workflows** (`.github/workflows/refresh.yml` for daily + intraday line/pick refreshes and `.github/workflows/backtest.yml` for bi-weekly walk-forward backtesting & self-tuning) with `actions/cache` warehouse persistence and automatic `gh-pages` deployment so all tasks execute even when the local Mac is asleep or powered off (plus `RunAtLoad=true` catch-up on local `launchd`).
