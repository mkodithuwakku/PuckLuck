# Runbook: Phase 2 — Yahoo Fantasy OAuth & Official NHL API Ingestion

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Implement `ingestion/yahoo.py`, `ingestion/nhl.py`, and their offline unit test suites `tests/test_yahoo.py` and `tests/test_nhl.py`.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and un-committed for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `db.py` — Warehouse connection & insert/upsert helpers (`upsert_player`, `upsert_team`, `insert_skater_game`, `insert_goalie_game`).
2. `ingestion/crosswalk.py` — `CrosswalkResolver`, `YahooIdentity`, `MatchResult`, `normalize_name`.
3. `conftest.py` — Offline socket blocker (`NetworkAccessBlocked`).

### Files to Create / Modify
- `ingestion/yahoo.py` (CREATE)
- `ingestion/nhl.py` (CREATE)
- `tests/test_yahoo.py` (CREATE)
- `tests/test_nhl.py` (CREATE)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/moneypuck.py`, `ingestion/crosswalk.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Module 1 Specification: `ingestion/yahoo.py` (SRS §3.1 Source 1, §7.1)

### 2.1 Constants & Transport Seam
- `YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"`
- `YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"`
- `YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"`
- `REDIRECT_URI_OOB = "oob"`
- **Transport types (offline seam — never open sockets in unit tests):**
  - `TokenTransport = Callable[[str, Dict[str, str], Dict[str, str]], Dict[str, Any]]`  
    *(args: `url`, `headers`, `form_data` -> parsed JSON dict)*
  - `ApiTransport = Callable[[str, Dict[str, str]], Dict[str, Any]]`  
    *(args: `url`, `headers` -> parsed JSON dict)*
  - Provide default production implementations using `urllib.request` (matching `ingestion/moneypuck.py`), while allowing tests to inject stub callables.

### 2.2 Canonical Stat Mapping
Yahoo NHL fantasy stat IDs map to PuckLuck canonical stat keys (used by Phase 3 `ScoringRescorer`):
```python
YAHOO_STAT_ID_MAP: Dict[int, str] = {
    1: "G",
    2: "A",
    4: "PLUS_MINUS",
    5: "PIM",
    8: "PPP",
    9: "SHP",
    11: "GWG",
    14: "SOG",
    16: "FW",
    31: "HIT",
    32: "BLK",
    19: "W",
    22: "GA",
    25: "SV",
    27: "SHO",
}
```
Also accept string stat names/abbreviations (`"G"`, `"A"`, `"SOG"`, `"HIT"`, `"BLK"`, `"PPP"`, `"SHP"`, `"FW"`, `"PIM"`, `"+/-"`, `"W"`, `"GA"`, `"SV"`, `"SHO"`) when `stat_id` is absent or custom, normalizing `"+/-"` -> `"PLUS_MINUS"`.

### 2.3 Data Contracts (`@dataclass(frozen=True)`)
```python
@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: float  # unix epoch seconds (time.time() + expires_in)
    token_type: str = "bearer"

    def is_expired(self, *, now: Optional[float] = None, skew_seconds: float = 60.0) -> bool:
        current = time.time() if now is None else now
        return current + skew_seconds >= self.expires_at


@dataclass(frozen=True)
class LeagueSettings:
    league_key: str
    league_name: str
    stat_weights: Dict[str, float]          # canonical stat key -> fantasy point weight
    roster_positions: Dict[str, int]        # slot ("C","LW","RW","D","UTIL","G","BN","IR","IR+") -> count
    max_weekly_adds: Optional[int] = None


@dataclass(frozen=True)
class YahooPlayer:
    yahoo_player_id: str                    # e.g. "449.p.6743"
    display_name: str                       # e.g. "Connor McDavid"
    primary_position: str                   # first non-slot position, e.g. "C"
    eligible_positions: Tuple[str, ...]     # e.g. ("C", "UTIL")
    team_abbr: Optional[str] = None         # uppercase, e.g. "EDM"
    selected_slot: Optional[str] = None     # active roster slot ("C", "BN", "IR+", etc.) or None for FA
    status: Optional[str] = None            # e.g. "IR", "DTD", None
    percent_owned: Optional[float] = None
    player_id: Optional[int] = None         # resolved NHL player_id once crosswalked
```

### 2.4 Required Functions & Class Signatures
1. `build_authorization_url(client_id: str, redirect_uri: str = REDIRECT_URI_OOB) -> str`
   - Returns URL with query params `client_id`, `redirect_uri`, `response_type=code`.
2. ` persist_refresh_token(env_path: Union[str, Path], refresh_token: str) -> None`
   - Atomically updates `YAHOO_REFRESH_TOKEN=<refresh_token>` inside `env_path` (creating the file or appending the key if missing, preserving all other lines and comments, using a `.tmp` file + `os.replace`). Rejects tokens containing newlines (`ValueError`).
3. `class YahooClient:`
   ```python
   class YahooClient:
       def __init__(
           self,
           client_id: str,
           client_secret: str,
           refresh_token: str,
           *,
           env_path: Optional[Union[str, Path]] = None,
           token_transport: TokenTransport = default_token_transport,
           api_transport: ApiTransport = default_api_transport,
           clock: Callable[[], float] = time.time,
       ) -> None: ...
   ```
   - `exchange_code(self, code: str, redirect_uri: str = REDIRECT_URI_OOB) -> OAuthTokens`: Exchanges out-of-band `authorization_code` for tokens using HTTP Basic auth (`base64(client_id:client_secret)`), stores tokens on the client, and if `env_path` is set, persists the new `refresh_token`.
   - `refresh_access_token(self) -> OAuthTokens`: Uses `grant_type=refresh_token`. Updates `self._tokens` and, if `env_path` is set and the returned `refresh_token` is non-empty, calls `persist_refresh_token`.
   - `ensure_token(self) -> str`: Returns valid `access_token`, calling `refresh_access_token()` only when no token exists or `is_expired(now=self._clock())` is True.
   - `get_json(self, resource_path: str) -> Dict[str, Any]`: Calls `{YAHOO_API_BASE}/{resource_path.lstrip('/')}?format=json` (or appends `&format=json` if `?` already in `resource_path`) with header `Authorization: Bearer <access_token>`.
   - `fetch_league_settings(self, league_key: str) -> LeagueSettings`: Calls `league/{league_key}/settings` and parses `parse_league_settings(payload, league_key=league_key)`.
   - `fetch_roster(self, team_key: str) -> List[YahooPlayer]`: Calls `team/{team_key}/roster` and parses `parse_roster(payload)`.
   - `fetch_free_agents(self, league_key: str, *, start: int = 0, count: int = 25) -> List[YahooPlayer]`: Calls `league/{league_key}/players;status=FA;start={start};count={count};out=ownership` and parses `parse_free_agents(payload)`.
   - `sync_players_to_db(self, conn: sqlite3.Connection, players: Sequence[YahooPlayer]) -> List[YahooPlayer]`: Runs `CrosswalkResolver(conn).link_all(...)` over `players` and returns a new list of `YahooPlayer` instances with `player_id` populated from the `MatchResult`.
4. **Pure Parser Functions (support both Yahoo's nested `fantasy_content` JSON structure and flat normalized test dicts):**
   - `parse_league_settings(payload: Dict[str, Any], *, league_key: Optional[str] = None) -> LeagueSettings`
   - `parse_roster(payload: Dict[str, Any]) -> List[YahooPlayer]`
   - `parse_free_agents(payload: Dict[str, Any]) -> List[YahooPlayer]`

---

## 3. Module 2 Specification: `ingestion/nhl.py` (SRS §3.1 Source 2, §3.2)

### 3.1 Constants & Transport Seam
- `NHL_API_BASE = "https://api-web.nhle.com/v1"`
- `USER_AGENT = "PuckLuck/0.1 (local analytics pipeline)"`
- `JsonFetcher = Callable[[str], Dict[str, Any]]` (injectable offline seam; default uses `urllib.request` with `USER_AGENT`).
- Known altitude lookup (feet above sea level for `dim_teams.altitude_ft`):
  ```python
  TEAM_ALTITUDE_FT: Dict[str, int] = {
      "COL": 5280,
      "UTA": 4226,
      "VGK": 2001,
      "CGY": 3438,
      "EDM": 2208,
      "WPG": 784,
  }
  ```
  (All other teams default to `0`.)

### 3.2 Data Contracts (`@dataclass(frozen=True)`)
```python
@dataclass(frozen=True)
class ScheduledGame:
    game_id: int
    season: int
    game_date: str          # YYYY-MM-DD
    start_time_utc: str     # ISO-8601 UTC timestamp
    home_team_id: int
    home_abbr: str
    home_name: str
    away_team_id: int
    away_abbr: str
    away_name: str
    venue: str
```

### 3.3 Required Functions & Signatures
1. `parse_toi_to_seconds(toi: Optional[Union[str, int, float]]) -> int`
   - Converts `"MM:SS"` (e.g. `"21:34"` -> `1294`, `"0:45"` -> `45`) or numeric seconds to `int`. Returns `0` for `None` or `""`. Raises `ValueError` on malformed strings.
2. `normalize_nhl_position(pos_code: Optional[str]) -> str`
   - Maps NHL API position codes: `"L"` -> `"LW"`, `"R"` -> `"RW"`, `"C"` -> `"C"`, `"D"` -> `"D"`, `"G"` -> `"G"`. (Already-normalized `"LW"`/`"RW"` stay unchanged; unknown defaults to `"C"`).
3. `compute_rest_days(conn: sqlite3.Connection, team_id: int, game_date: str, *, default_rest: int = 2) -> int`
   - Queries `SELECT MAX(game_date) FROM fact_skater_games WHERE team_id = ? AND game_date < ?` (strictly `< game_date` for temporal integrity, SRS §8).
   - If no prior game exists, returns `default_rest` (`2`).
   - Otherwise returns `max(0, (date.fromisoformat(game_date) - date.fromisoformat(prev_date)).days - 1)` (so playing on consecutive days `2026-10-21` and `2026-10-22` yields `rest_days = 0`, one off-day in between yields `1`, etc.).
4. `fetch_schedule(target_date: Union[str, date], *, fetcher: JsonFetcher = default_json_fetcher) -> List[ScheduledGame]`
   - Requests `{NHL_API_BASE}/schedule/{yyyy-mm-dd}`.
   - Parses `gameWeek` (filtering to games matching `target_date` if multiple dates are returned in the week block, or reading top-level `games` if present).
5. `ingest_team_roster(conn: sqlite3.Connection, team_abbr: str, *, fetcher: JsonFetcher = default_json_fetcher, commit: bool = True) -> int`
   - Requests `{NHL_API_BASE}/roster/{team_abbr.upper()}/current`.
   - Extracts `forwards`, `defensemen`, `goalies` arrays.
   - Upserts each player via `db.upsert_player` using `normalize_name(full_name)` for `canonical_name` and `normalize_nhl_position(positionCode)` for `primary_position`.
   - Returns count of players upserted.
6. `ingest_boxscore(conn: sqlite3.Connection, game_id: int, *, fetcher: JsonFetcher = default_json_fetcher, advanced_metrics: Optional[Dict[int, Dict[str, float]]] = None, commit: bool = True) -> Tuple[int, int]`
   - Requests `{NHL_API_BASE}/gamecenter/{game_id}/boxscore`.
   - Upserts home and away teams into `dim_teams` via `db.upsert_team` (with `altitude_ft=TEAM_ALTITUDE_FT.get(abbr, 0)`).
   - Computes `rest_days` for home and away teams BEFORE inserting today's skater rows.
   - Iterates `playerByGameStats.{awayTeam,homeTeam}.{forwards,defense}`:
     - Upserts player into `dim_players` (so FK never fails even for call-ups).
     - Parses `toi` -> `toi_seconds`, `powerPlayToi` -> `pp_toi_seconds` (default `"0:00"`), `goals`, `assists`, `sog` (or `shots`), `blockedShots` (or `blocks`), `hits`, `faceoffWinningPctg` / `faceoffsWon` (`faceoffs_won` int, default `0`), `pim`.
     - Merges optional per-player `advanced_metrics.get(player_id, {})` for `ixg`, `icorsi`, `ihdcf`, `on_ice_xg_share` (defaulting to `0.0` / `0`), and calls `db.insert_skater_game`.
   - Iterates `playerByGameStats.{awayTeam,homeTeam}.goalies`:
     - Upserts goalie into `dim_players` with `primary_position="G"`.
     - Skips dressed backup goalies with `toi_seconds == 0`.
     - Computes `saves = max(0, shots_against - goals_against)` if `saves` not explicitly integer, merges `gsax` (default `0.0`) and `hd_save_pct` (default `None`) from `advanced_metrics`, and calls `db.insert_goalie_game`.
   - Returns `(skaters_inserted, goalies_inserted)`.

---

## 4. Required Unit Tests

### `tests/test_yahoo.py`
1. `test_build_authorization_url`: Verifies query parameters (`client_id`, `redirect_uri=oob`, `response_type=code`).
2. `test_token_refresh_and_env_persistence`: Verifies `YahooClient.ensure_token()` refreshes an expired token via stubbed `token_transport`, sends Basic Auth header, and atomically updates `YAHOO_REFRESH_TOKEN` in a `tmp_path / ".env"` file while preserving existing comments and `YAHOO_CLIENT_ID`.
3. `test_ensure_token_reuses_valid_token`: Verifies `token_transport` is NOT called when current token has not expired.
4. `test_parse_league_settings`: Uses a realistic Yahoo nested `fantasy_content` JSON fixture to verify `stat_weights` (`G`, `A`, `SOG`, `HIT`, `BLK`, `PPP`, `W`, `GA`, `SV`, `SHO`), `roster_positions` (`C`, `LW`, `RW`, `D`, `G`, `BN`, `IR+`), and `max_weekly_adds`.
5. `test_fetch_roster_and_sync_to_db`: Stubs `api_transport` for `/team/{team_key}/roster`, parses active lineup slots (`C`, `BN`, `IR+`), and verifies `sync_players_to_db` links Yahoo IDs onto seeded `dim_players` rows via `CrosswalkResolver`.
6. `test_fetch_free_agents`: Stubs `api_transport` for `/league/{league_key}/players;status=FA...`, verifies `percent_owned` and `status` extraction.
7. `test_persist_refresh_token_rejects_newline`: Verifies injection guard raises `ValueError`.

### `tests/test_nhl.py`
1. `test_parse_toi_to_seconds`: Tests `"21:34"` -> `1294`, `"00:00"` -> `0`, `None` -> `0`, and invalid string raises `ValueError`.
2. `test_normalize_nhl_position`: Tests `"L"` -> `"LW"`, `"R"` -> `"RW"`, `"C"` -> `"C"`, `"D"` -> `"D"`, `"G"` -> `"G"`.
3. `test_fetch_schedule_filters_target_date`: Stubs `/v1/schedule/2026-10-23` multi-day `gameWeek` fixture and verifies only `2026-10-23` games are returned as `ScheduledGame`.
4. `test_ingest_team_roster_populates_dim_players`: Stubs `/v1/roster/EDM/current` and verifies forwards, defensemen, and goalies are upserted into `dim_players` with normalized canonical names and positions (`L` -> `LW`).
5. `test_compute_rest_days_temporal_integrity`: Seeds games on `2026-10-20`, `2026-10-21`, and a future game on `2026-10-25`; verifies `compute_rest_days` for `2026-10-22` returns `0` (B2B), for `2026-10-23` returns `1`, and ignores the `2026-10-25` future row.
6. `test_ingest_boxscore_populates_facts_and_skips_zero_toi_goalie`: Stubs `/v1/gamecenter/2026020001/boxscore`, passes `advanced_metrics`, and verifies:
   - `dim_teams` has `COL` with `altitude_ft == 5280` and `EDM` with `altitude_ft == 2208`.
   - `fact_skater_games` row has expected `toi_seconds`, `pp_toi_seconds`, counting stats, and `ixg`/`icorsi`.
   - `fact_goalie_games` includes the starting goalie (`toi_seconds > 0`) with `gsax` and excludes the `00:00` backup goalie.

---

## 5. Verification Gate (Run Before Completing Session)
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
```
Both commands MUST pass with zero errors. Stage all new files with `git add`.
