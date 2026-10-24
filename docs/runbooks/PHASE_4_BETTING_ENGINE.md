# Runbook: Phase 4 — Bivariate Poisson Game Totals & +EV Market Screener

**Role:** Agent 2 (Coding / Implementation Agent)  
**Scope:** Implement `betting/__init__.py`, `betting/poisson.py`, `betting/ev.py`, and their deterministic offline test suites `tests/test_poisson.py` and `tests/test_ev.py`.  
**Protocol (SRS §8.1):** Read ONLY the files listed under **Readable Context**. Do NOT modify protected files. Leave all changes staged (`git add`) and uncommitted for Agent 1 review.

---

## 1. File Permissions

### Readable Context (Read ONLY these files)
1. `docs/runbooks/PHASE_4_BETTING_ENGINE.md` — This specification.
2. `db.py` — SQLite schema (`fact_market_lines`, `fact_skater_games`, `fact_goalie_games`) and `insert_market_line`.
3. `features/builder.py` — `compute_ewma`, `team_rest_days`, `EWMA_ALPHA`.

### Files to Create / Modify
- `betting/__init__.py` (CREATE)
- `betting/poisson.py` (CREATE)
- `betting/ev.py` (CREATE)
- `tests/test_poisson.py` (CREATE)
- `tests/test_ev.py` (CREATE)

### Strictly Protected Files (DO NOT MODIFY)
- `db.py`, `conftest.py`, `ingestion/*`, `features/*`, `models/*`, `fantasy/*`, existing `tests/test_*.py`, `AGENTS.md`, `docs/*`, `pyproject.toml`, `requirements.txt`

---

## 2. Module 1 Specification: `betting/poisson.py` (SRS §5.1, §5.2)

### 2.1 Constants & Mathematical Formulation
- `DEFAULT_BASE_RATE: float = 3.05` (league baseline goals per team per game)
- `DEFAULT_VENUE_HOME: float = 1.04` (home ice scoring multiplier)
- `UNCONFIRMED_GOALIE_PENALTY: float = 0.22` (SRS §5.1: $+0.22\text{ goals to }\lambda$ when opposing goalie is unconfirmed)
- `MIN_LAMBDA: float = 0.05` (lower bound on $\lambda_H, \lambda_A$)
- `MAX_GOALS_GRID: int = 15` (SRS §5.2: $H \in [0, 15], A \in [0, 15]$)

#### Expected Goal Intensities (SRS §5.1):
$$\lambda_H = \text{BaseRate} \times \text{TeamOffense}_H \times \text{OpponentDefense}_A \times \text{VenueHome} \times \text{RestFactor}_H \times \text{GoalieAdjustment}_A + \mathbb{I}(\neg \text{away\_goalie\_confirmed}) \cdot 0.22$$
$$\lambda_A = \text{BaseRate} \times \text{TeamOffense}_A \times \text{OpponentDefense}_H \times \text{RestFactor}_A \times \text{GoalieAdjustment}_H + \mathbb{I}(\neg \text{home\_goalie\_confirmed}) \cdot 0.22$$

Where:
- `goalie_adjustment(gsax_per60: float) -> float`:
  $$\text{GoalieAdjustment} = 1.0 - \left(\frac{\text{GSAx}/60}{3.0}\right)$$
  Clamped to `[0.5, 1.5]` so extreme single-game outliers cannot invert $\lambda$.
- `rest_factor(rest_days: int) -> float`:
  - `0` (Back-to-Back): `0.95`
  - `1` (1 day rest): `1.00`
  - `>= 2` (2+ days rest): `1.02`
- Both $\lambda_H$ and $\lambda_A$ are clamped to `max(MIN_LAMBDA, value)`.

#### Total Goal Line PMF Grid (SRS §5.2):
Evaluated across $h \in [0, 15], a \in [0, 15]$:
$$P(H = h) = \frac{\lambda_H^h e^{-\lambda_H}}{h!}, \quad P(A = a) = \frac{\lambda_A^a e^{-\lambda_A}}{a!}$$
$$P(\text{Over } L) = \sum_{h=0}^{15} \sum_{a=0}^{15} \mathbb{I}(h + a > L) \cdot P(H = h) \cdot P(A = a)$$
$$P(\text{Under } L) = \sum_{h=0}^{15} \sum_{a=0}^{15} \mathbb{I}(h + a < L) \cdot P(H = h) \cdot P(A = a)$$
$$P(\text{Push } L) = \sum_{h=0}^{15} \sum_{a=0}^{15} \mathbb{I}(h + a == L) \cdot P(H = h) \cdot P(A = a)$$

### 2.2 Data Contracts & Required Functions
```python
@dataclass(frozen=True)
class TeamMatchupInputs:
    team_offense: float = 1.0          # relative to league avg (1.0 = avg)
    opponent_defense: float = 1.0      # opposing team xGA relative to league avg (1.0 = avg)
    rest_days: int = 2
    opposing_goalie_gsax_per60: float = 0.0
    opposing_goalie_confirmed: bool = True


@dataclass(frozen=True)
class TotalLinePrice:
    line: float
    prob_over: float
    prob_under: float
    prob_push: float
    lambda_home: float
    lambda_away: float

    @property
    def expected_total(self) -> float:
        return float(self.lambda_home + self.lambda_away)


def goalie_adjustment(gsax_per60: float) -> float: ...
def rest_factor(rest_days: int) -> float: ...

def compute_expected_goals(
    home: TeamMatchupInputs,
    away: TeamMatchupInputs,
    *,
    base_rate: float = DEFAULT_BASE_RATE,
    venue_home: float = DEFAULT_VENUE_HOME,
) -> Tuple[float, float]:
    """Return (lambda_home, lambda_away) per SRS §5.1."""

def poisson_pmf_grid(
    lambda_home: float,
    lambda_away: float,
    *,
    max_goals: int = MAX_GOALS_GRID,
) -> np.ndarray:
    """Return (max_goals+1, max_goals+1) joint probability matrix P[h, a]."""

def price_total_goals(
    lambda_home: float,
    lambda_away: float,
    line: float,
    *,
    max_goals: int = MAX_GOALS_GRID,
) -> TotalLinePrice:
    """Evaluate P(Over L), P(Under L), P(Push L) across h, a in [0, max_goals]."""

def build_matchup_inputs_from_db(
    conn: sqlite3.Connection,
    home_team_id: int,
    away_team_id: int,
    as_of_date: str,
    *,
    home_goalie_id: Optional[int] = None,
    away_goalie_id: Optional[int] = None,
    home_goalie_confirmed: bool = True,
    away_goalie_confirmed: bool = True,
) -> Tuple[TeamMatchupInputs, TeamMatchupInputs]:
    """Derive (home_inputs, away_inputs) strictly from warehouse rows < as_of_date.

    - TeamOffense_T = mean per-game SUM(ixg) generated by team T / league mean per-game xG (default 1.0 if no history).
    - OpponentDefense_O = mean per-game SUM(ixg) allowed by team O / league mean per-game xG (default 1.0 if no history).
    - rest_days via team_rest_days(conn, team_id, as_of_date).
    - opposing_goalie_gsax_per60 = EWMA(alpha=EWMA_ALPHA) of gsax*3600/toi_seconds for the opposing goalie (or most recent goalie for that team if goalie_id is None) strictly before as_of_date.
    """
```

---

## 3. Module 2 Specification: `betting/ev.py` (SRS §5.3, `ARCHITECTURE_MAP.md`)

### 3.1 Constants & Mathematical Formulation
- `MIN_EV_EDGE: float = 0.035` ($+3.5\%$ edge threshold per SRS §5.3)
- `MARKET_TOTAL_GOALS = "TOTAL_GOALS"`
- `LOW_LIQUIDITY_BOOKS: FrozenSet[str] = frozenset({"offshore_low", "promo_only", "social_book"})`

#### Odds Conversion & Expected Value (SRS §5.3):
- `american_to_decimal(american_odds: int) -> float`:
  - Raises `ValueError` if `abs(int(american_odds)) < 100`.
  - If `american_odds > 0`: `1.0 + (float(american_odds) / 100.0)`
  - If `american_odds < 0`: `1.0 + (100.0 / abs(float(american_odds)))`
- `compute_ev(model_prob: float, decimal_odds: float) -> float`:
  $$\text{EV} = (P_{\text{model}} \times O_{\text{book}}) - 1.0$$
  *(Note: when Push probability $p_{\text{push}} > 0$ on an integer line, a two-way no-push win probability or raw `model_prob * decimal_odds - 1.0` may be asked; follow SRS §5.3 Eq. 250 literally: `EV = (model_prob * decimal_odds) - 1.0`.)*

### 3.2 Data Contracts & Required Functions
```python
@dataclass(frozen=True)
class MarketTotalLine:
    game_id: int
    sportsbook: str
    line: float
    over_odds: int          # American odds, e.g. -110
    under_odds: int         # American odds, e.g. -110
    captured_at: str


@dataclass(frozen=True)
class BetRecommendation:
    game_id: int
    sportsbook: str
    side: str               # "OVER" or "UNDER"
    line: float
    american_odds: int
    decimal_odds: float
    model_prob: float
    expected_total: float
    ev: float
    is_positive_ev: bool    # True iff ev >= min_edge


def upsert_total_line(
    conn: sqlite3.Connection,
    market: MarketTotalLine,
    *,
    commit: bool = True,
) -> None:
    """Persist a game-total line into fact_market_lines without NULL target_id duplicates.

    Deletes any prior row matching (game_id, sportsbook, market_type='TOTAL_GOALS')
    where target_id IS NULL before calling db.insert_market_line.
    """


def consensus_total_line(
    lines: Sequence[MarketTotalLine],
    *,
    excluded_books: Collection[str] = LOW_LIQUIDITY_BOOKS,
) -> Optional[MarketTotalLine]:
    """Filter out excluded_books (case-insensitive) and return a consensus MarketTotalLine.

    - Returns None if no eligible lines remain.
    - Consensus `line` = median of eligible `line` values (rounded to nearest 0.5).
    - Consensus `over_odds` / `under_odds` = best (highest American odds) available among eligible books whose `line` equals the consensus `line` (or across all eligible books if none match the exact rounded median).
    - `sportsbook` = "consensus", `captured_at` = max(captured_at) across eligible books.
    """


def evaluate_total_market(
    price: TotalLinePrice,
    market: MarketTotalLine,
    *,
    min_edge: float = MIN_EV_EDGE,
) -> Tuple[BetRecommendation, BetRecommendation]:
    """Return (over_rec, under_rec) evaluated against SRS §5.3 EV formula."""


def screen_positive_ev(
    evaluations: Sequence[Tuple[TotalLinePrice, MarketTotalLine]],
    *,
    min_edge: float = MIN_EV_EDGE,
    excluded_books: Collection[str] = LOW_LIQUIDITY_BOOKS,
) -> List[BetRecommendation]:
    """Return all BetRecommendations with is_positive_ev=True from eligible books, sorted by ev DESC."""
```

---

## 4. Required Unit Tests

### `tests/test_poisson.py`
1. `test_goalie_adjustment_and_unconfirmed_backup_penalty` (**SRS §5.1**):
   - Verifies `goalie_adjustment(1.5) == pytest.approx(1.0 - 1.5/3.0) == 0.5`.
   - Verifies `compute_expected_goals` adds exactly `+0.22` to `lambda_home` when `away_goalie_confirmed=False` and `+0.22` to `lambda_away` when `home_goalie_confirmed=False`.
2. `test_price_total_goals_grid_properties` (**SRS §5.2**):
   - For half-goal line `6.5`, verifies `prob_push == 0.0` and `prob_over + prob_under == pytest.approx(1.0, abs=1e-4)`.
   - For integer line `6.0`, verifies `prob_push > 0.0` and `prob_over + prob_under + prob_push == pytest.approx(1.0, abs=1e-4)`.
   - Verifies higher $\lambda_H + \lambda_A$ strictly increases `prob_over(6.5)`.
3. `test_build_matchup_inputs_from_db_temporal_integrity`:
   - Seeds historical games strictly before `2026-10-23` and future outlier games on `2026-10-23`; verifies `build_matchup_inputs_from_db(..., as_of_date="2026-10-23")` is unaffected by the `>= 2026-10-23` rows.

### `tests/test_ev.py`
1. `test_american_to_decimal_and_ev_formula` (**SRS §5.3**):
   - Verifies `-110 -> 1.9090909`, `+120 -> 2.20`, `+100 -> 2.00`, and invalid `50` raises `ValueError`.
   - Verifies `compute_ev(0.55, 2.00) == pytest.approx(0.10)`.
2. `test_ev_gate_enforces_3_5_pct_threshold_and_filters_low_liquidity` (**SRS §5.3**):
   - Evaluates a line with `+3.4%` edge (`is_positive_ev == False`) vs `+3.6%` edge (`is_positive_ev == True`).
   - Verifies `screen_positive_ev` excludes `LOW_LIQUIDITY_BOOKS` (`"offshore_low"`) and sorts surviving edges by `ev` descending.
3. `test_upsert_total_line_dedupes_null_target_id`:
   - Calls `upsert_total_line` twice for `(game_id=1, sportsbook="pinnacle")` with updated odds and verifies `fact_market_lines` contains exactly `1` row with the latest odds.
4. `test_consensus_total_line`:
   - Verifies low-liquidity books are ignored and consensus line/odds are computed deterministically.

---

## 5. Verification Gate (Run Before Completing Session)
```bash
.venv/bin/python -m pytest
.venv/bin/black --check .
```
Both commands MUST pass with zero errors. Stage all new files with `git add` and leave uncommitted for Agent 1 review.
