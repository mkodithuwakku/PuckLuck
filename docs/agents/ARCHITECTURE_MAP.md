# PuckLuck Architecture Map

Mirrors SRS §2–§3. Read this before touching any module so changes stay
consistent with the two-engine data flow.

## Data Flow

```
Yahoo Fantasy API (OAuth) ─┐
Official NHL API (REST)   ─┼─> ingestion/ ──> data/puckluck.sqlite3
MoneyPuck bulk CSV/parquet ┘                     │
                                               ▼
                            features/builder.py (15-game EWMA, pace,
                            opponent DefRtg, goalie GSAx suppression)
                                   │                          │
                    Sub-System A: fantasy/*     Sub-System B: betting/*
                    component ridge +           bivariate Poisson +
                    H-week add/drop solver      +EV total-goal pricing
                                   │                          │
                                   └──────────┬───────────────┘
                                              ▼
                       dist/report.json (allowlisted) -> gh-pages PWA
                       api/ FastAPI loopback 127.0.0.1:8877
```

## Module Responsibility Table

| Module                    | Responsibility                                        | Status   |
|---------------------------|-------------------------------------------------------|----------|
| `db.py`                   | Schema DDL, `connect()`/`init_db()`, typed row helpers| Phase 1  |
| `ingestion/moneypuck.py`  | Daily skaters/goalies CSV → parquet cache             | Phase 1  |
| `ingestion/crosswalk.py`  | Yahoo↔NHL identity resolution into `dim_players`      | Phase 1  |
| `ingestion/yahoo.py`      | OAuth refresh, roster/settings/FA ingest              | Phase 2  |
| `ingestion/nhl.py`        | `/schedule`, `/roster`, `/gamecenter` boxscores       | Phase 2  |
| `features/builder.py`     | EWMA process metrics, pace, rest, matchup features    | Phase 3  |
| `models/ridge.py`         | Closed-form ridge `(XᵀX+λI)⁻¹Xᵀy`, nonneg clamp       | Phase 3  |
| `fantasy/solver.py`       | ΔValue add/drop gate (SRS §4.2), HOLD/RECOMMEND_SWAP  | Phase 3  |
| `betting/poisson.py`      | λ_home/λ_away, P(Over L) grid over h,a ∈ [0,15]       | Phase 4  |
| `betting/ev.py`           | EV = p·O−1 gate at +3.5%, consensus line parser       | Phase 4  |
| `api/`                    | FastAPI on 127.0.0.1:8877, restricted CORS            | Phase 5  |
| `web/`                    | Roster-first PWA, 4 tabs (SRS §6.1)                   | Phase 5  |
| `scripts/`                | launchd plist, daily refresh, gh-pages publish        | Phase 5  |

## SQLite Schema (authoritative DDL in `db.py::SCHEMA_SQL`)

- `dim_players(player_id PK, yahoo_player_id UNIQUE, canonical_name,
  display_name, primary_position, shoots_catches, birth_date)` — index on
  `canonical_name`.
- `dim_teams(team_id PK, abbreviation UNIQUE, team_name, venue,
  altitude_ft)` — `altitude_ft` feeds fatigue modeling (DEN = 5280).
- `fact_skater_games` — PK `(game_id, player_id)`; FKs to `dim_players`
  and `dim_teams` ×2; counting stats + `ixg`, `icorsi`, `ihdcf`,
  `on_ice_xg_share`; indexes on `game_date` and `(player_id, game_date)`.
- `fact_goalie_games` — PK `(game_id, goalie_id)`; `decision`, shots/GA/
  saves, `gsax`, `hd_save_pct`.
- `fact_market_lines` — PK `(game_id, sportsbook, market_type,
  target_id)`; American odds + `captured_at`. **Quirk:** NULL `target_id`
  (game totals) never conflicts on the PK — dedupe totals on
  `(game_id, sportsbook, market_type)` before insert.

## Source Contracts

- **MoneyPuck:** `seasonSummary/{season}/regular/{skaters,goalies}.csv`;
  `season` = starting year (`season_for_date` maps Jul–Dec → that year,
  Jan–Jun → prior). Custom `User-Agent` required. Cached as parquet at
  `data/cache/moneypuck/<season>/<dataset>.parquet` with a 20h TTL.
  Canonical metric map in `SKATER_METRIC_COLUMNS` / `GOALIE_METRIC_COLUMNS`
  (SRS names ↔ MoneyPuck columns, e.g. `gsax` → `goalsSavedAboveExpected`).
- **Crosswalk:** `dim_players` keyed by NHL `player_id` (MoneyPuck's
  `playerId` is the same namespace). Yahoo keys attach via
  `CrosswalkResolver`: exact → position-disambiguated → first-name alias
  → fuzzy (ratio ≥ 0.88). `attach()` clears any prior Yahoo key first, so
  re-linking never violates the UNIQUE constraint.
