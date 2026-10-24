# PuckLuck Workflows & Testing

## Verification by Subsystem

| Scope                        | Command                                        |
|------------------------------|------------------------------------------------|
| All Python unit tests        | `.venv/bin/python -m pytest`                   |
| Single file                  | `.venv/bin/python -m pytest tests/test_db.py`  |
| Formatting (check)           | `.venv/bin/black --check .`                    |
| Formatting (apply)           | `.venv/bin/black .`                            |
| Frontend policy tests        | `node --test web/tests/dashboard.test.mjs`     |

All three gates from SRS §8 must be green before merging a phase:
pytest (offline), black (100%), node --test (once `web/` exists).

## Offline Discipline

`conftest.py` autouse-patches `socket.socket`, `socket.create_connection`,
and `socket.getaddrinfo` to raise `NetworkAccessBlocked`. Code under test
must never open sockets directly:

- **MoneyPuck:** inject `fetcher` into `update_cache()` — a callable
  `(url: str) -> bytes`. Tests return canned CSV bytes.
- **Yahoo/NHL (Phase 2):** same pattern — a transport seam returning
  response objects/bytes; stub it in fixtures, never patch `requests`.

## Test Conventions

- DB tests use `db.init_db(":memory:")`; file-backed tests use pytest's
  `tmp_path`.
- Seed parents before facts: `fact_*` tables enforce FKs to
  `dim_players`/`dim_teams` (insert helpers `upsert_player`,
  `upsert_team`, `insert_skater_game`, `insert_goalie_game`,
  `insert_market_line`).
- Synthetic fixtures only — small inline CSVs/dicts, no real downloads.
- Cache-freshness tests: `os.utime` the parquet mtime into the past, or
  pass `max_age_hours=0`.

## Daily & Bi-Weekly Operations (Sleep-Proof Cloud + Local)

### 1. Sleep-Proof Cloud Execution (GitHub Actions — Runs While Mac Is Asleep/Off)
- `.github/workflows/refresh.yml` (Phase 8): Runs on GitHub-hosted runners on a scheduled `cron` (morning full slate + intraday odds/pick refreshes before puck drop) and `workflow_dispatch`. Restores `data/puckluck.sqlite3`, `data/cache/moneypuck`, and `data/model_params.json` via `actions/cache`, runs `python -m scripts.refresh --live`, validates `dist/report.json`, and deploys `web/` + `dist/report.json` to `gh-pages`.
- `.github/workflows/backtest.yml` (Phase 8): Runs every two weeks (`cron: '0 11 1,15 * *'`) in the cloud to evaluate 14-day out-of-sample accuracy & `+EV` ROI and auto-tune `data/model_params.json`.

### 2. Local macOS LaunchAgent (`com.puckluck.refresh.plist`, `RunAtLoad=true` Wake Catch-Up)

```bash
launchctl load   ~/Library/LaunchAgents/com.puckluck.refresh.plist
launchctl start  com.puckluck.refresh           # manual run
launchctl unload ~/Library/LaunchAgents/com.puckluck.refresh.plist
tail -f data/logs/refresh.log                   # stdout/stderr sink
```

Pipeline order: refresh Yahoo OAuth → pull rosters/settings → MoneyPuck
`update_cache()` → NHL boxscores → fit component ridges on `t < today` →
add/drop solver + Poisson totals → export allowlisted `dist/report.json`
→ push `gh-pages`.

## Runbooks

Atomic implementation prompts live in `docs/runbooks/<FEATURE>.md`
(SRS §8.1). Each names the 2–3 readable files, protected files, exact
signatures, and deterministic test fixtures.

- `docs/runbooks/PHASE_2_INGESTION.md` — Phase 2: Yahoo OAuth 2.0 (`ingestion/yahoo.py`) + Official NHL API (`ingestion/nhl.py`)
- `docs/runbooks/PHASE_3_FANTASY_ENGINE.md` — Phase 3: FeatureBuilder (`features/builder.py`), ComponentRidge (`models/ridge.py`), H-week Add/Drop Solver (`fantasy/solver.py`)
- `docs/runbooks/PHASE_4_BETTING_ENGINE.md` — Phase 4: Bivariate Poisson Goal Model (`betting/poisson.py`) + Consensus Odds & +EV Screener (`betting/ev.py`)
- `docs/runbooks/PHASE_5_DELIVERY_AND_PWA.md` — Phase 5: Allowlisted Report Export & LaunchAgent (`scripts/`), Loopback FastAPI (`api/`), Mobile PWA Dashboard & Node Policy Suite (`web/`)
- `docs/runbooks/PHASE_6_E2E_PIPELINE_AND_DEMO.md` — Phase 6: End-to-End Warehouse Orchestration, Yahoo-Optional Demo Mode (`scripts/refresh.py`) & Static PWA Mount (`api/server.py`)
