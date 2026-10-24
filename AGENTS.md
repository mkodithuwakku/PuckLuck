# AGENTS.md — PuckLuck

Institutional-grade NHL predictive analytics: Yahoo Fantasy roster optimization
(H-week asset-depreciation add/drop solver) + bivariate-Poisson game-total
pricing, served via local FastAPI and a zero-cost GitHub Pages PWA.

Source of truth: `docs/PUCKLUCK_SRS.md`. Follow it strictly — do not invent
undocumented schemas or scrape unstable pages.

## Environment

- Python **3.11+** via virtualenv: `.venv/bin/python` (currently 3.13)
- Install deps: `.venv/bin/pip install -r requirements.txt`
- Secrets live in `.env` (git-ignored; see `.env.example`). NEVER commit
  tokens, keys, or internal paths (SRS §7.1).

## Verification (run before any commit)

```bash
.venv/bin/python -m pytest          # unit suite — offline only, sockets blocked
.venv/bin/black .                   # deterministic formatter, 100% compliance
node --test web/tests/              # frontend policy tests (Phase 5)
```

## Invariants

- **Offline tests:** `conftest.py` patches `socket.socket`,
  `create_connection`, and `getaddrinfo`. All network access must flow
  through an injectable seam (e.g. `moneypuck.update_cache(fetcher=...)`).
- **Temporal integrity:** features for game *g* may only use rows with
  `game_date < g.game_date` (SRS §8: no future leakage).
- **Schema:** `db.py` DDL is the contract — five tables, composite PKs,
  `PRAGMA foreign_keys = ON` on every connection.
- **Static boundary:** `dist/report.json` is allowlisted fields only; it is
  git-ignored and published solely via the `gh-pages` branch.

## File Routing Index

| Feature                            | Module                          |
|------------------------------------|---------------------------------|
| SQLite schema & connection         | `db.py`                         |
| MoneyPuck CSV → parquet cache      | `ingestion/moneypuck.py`        |
| Yahoo↔NHL player identity          | `ingestion/crosswalk.py`        |
| Yahoo OAuth + roster ingest        | `ingestion/yahoo.py` (Phase 2)  |
| NHL API ingest                     | `ingestion/nhl.py`   (Phase 2)  |
| EWMA feature builder               | `features/builder.py` (Phase 3) |
| Component ridge regression         | `models/ridge.py`    (Phase 3)  |
| H-week add/drop solver             | `fantasy/solver.py`  (Phase 3)  |
| Bivariate Poisson totals           | `betting/poisson.py` (Phase 4)  |
| +EV market screener                | `betting/ev.py`      (Phase 4)  |
| launchd plist + publish pipeline   | `scripts/`           (Phase 5)  |
| Local API (127.0.0.1:8877)         | `api/`               (Phase 5)  |
| Mobile dashboard / PWA             | `web/`               (Phase 5)  |
| Static export                      | `dist/report.json`   (Phase 5)  |

## Two-Agent Dual-Engine Pattern (SRS §8)

Implement ONE bounded runbook per session from `docs/runbooks/<FEATURE>.md`.
Read only the files the runbook names. Agent docs live in `docs/agents/`:
`ARCHITECTURE_MAP.md` (modules, schema, data flow) and
`WORKFLOWS_AND_TESTING.md` (verification, mocks, launchd ops).
