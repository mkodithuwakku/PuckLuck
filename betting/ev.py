"""+EV market screener & consensus odds (SRS §5.3).

Evaluates modeled Over/Under probabilities against American-odds market
lines: ``EV = P_model * O_decimal - 1.0`` and flags opportunities only
when ``EV >= +0.035``. Low-liquidity books are excluded from consensus
and screening. ``upsert_total_line`` dedupes NULL ``target_id`` rows —
SQLite composite PKs never conflict on NULLs, so a manual DELETE is
required before insert.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import Collection, FrozenSet, List, Optional, Sequence, Tuple

import db
from betting.poisson import TotalLinePrice

MIN_EV_EDGE: float = 0.035
MARKET_TOTAL_GOALS = "TOTAL_GOALS"
LOW_LIQUIDITY_BOOKS: FrozenSet[str] = frozenset(
    {"offshore_low", "promo_only", "social_book"}
)


@dataclass(frozen=True)
class MarketTotalLine:
    game_id: int
    sportsbook: str
    line: float
    over_odds: int  # American odds, e.g. -110
    under_odds: int  # American odds, e.g. -110
    captured_at: str


@dataclass(frozen=True)
class BetRecommendation:
    game_id: int
    sportsbook: str
    side: str  # "OVER" or "UNDER"
    line: float
    american_odds: int
    decimal_odds: float
    model_prob: float
    expected_total: float
    ev: float
    is_positive_ev: bool  # True iff ev >= min_edge


def american_to_decimal(american_odds: int) -> float:
    """Convert American odds to decimal odds; |odds| must be >= 100."""
    value = int(american_odds)
    if abs(value) < 100:
        raise ValueError(f"invalid American odds: {american_odds!r}")
    if value > 0:
        return 1.0 + value / 100.0
    return 1.0 + 100.0 / abs(float(value))


def compute_ev(model_prob: float, decimal_odds: float) -> float:
    """``EV = (P_model * O_book) - 1.0`` (SRS §5.3)."""
    return float(model_prob) * float(decimal_odds) - 1.0


def upsert_total_line(
    conn: sqlite3.Connection,
    market: MarketTotalLine,
    *,
    commit: bool = True,
) -> None:
    """Persist a game-total line, deduping the NULL target_id PK quirk."""
    conn.execute(
        "DELETE FROM fact_market_lines "
        "WHERE game_id = ? AND sportsbook = ? AND market_type = ? "
        "AND target_id IS NULL",
        (market.game_id, market.sportsbook, MARKET_TOTAL_GOALS),
    )
    db.insert_market_line(
        conn,
        {
            "game_id": market.game_id,
            "sportsbook": market.sportsbook,
            "market_type": MARKET_TOTAL_GOALS,
            "target_id": None,
            "line": market.line,
            "over_odds": market.over_odds,
            "under_odds": market.under_odds,
            "captured_at": market.captured_at,
        },
    )
    if commit:
        conn.commit()


def consensus_total_line(
    lines: Sequence[MarketTotalLine],
    *,
    excluded_books: Collection[str] = LOW_LIQUIDITY_BOOKS,
) -> Optional[MarketTotalLine]:
    """Median line / best-price consensus across eligible books."""
    excluded = {b.lower() for b in excluded_books}
    eligible = [l for l in lines if l.sportsbook.lower() not in excluded]
    if not eligible:
        return None

    median = statistics.median(l.line for l in eligible)
    consensus_line = round(median * 2) / 2  # nearest 0.5

    on_line = [l for l in eligible if l.line == consensus_line]
    odds_pool = on_line if on_line else eligible

    return MarketTotalLine(
        game_id=eligible[0].game_id,
        sportsbook="consensus",
        line=consensus_line,
        over_odds=max(l.over_odds for l in odds_pool),
        under_odds=max(l.under_odds for l in odds_pool),
        captured_at=max(l.captured_at for l in eligible),
    )


def evaluate_total_market(
    price: TotalLinePrice,
    market: MarketTotalLine,
    *,
    min_edge: float = MIN_EV_EDGE,
) -> Tuple[BetRecommendation, BetRecommendation]:
    """Return (over_rec, under_rec) for one priced line at one book."""
    over_dec = american_to_decimal(market.over_odds)
    under_dec = american_to_decimal(market.under_odds)
    over_ev = compute_ev(price.prob_over, over_dec)
    under_ev = compute_ev(price.prob_under, under_dec)
    over_rec = BetRecommendation(
        game_id=market.game_id,
        sportsbook=market.sportsbook,
        side="OVER",
        line=market.line,
        american_odds=market.over_odds,
        decimal_odds=over_dec,
        model_prob=price.prob_over,
        expected_total=price.expected_total,
        ev=over_ev,
        is_positive_ev=over_ev >= min_edge,
    )
    under_rec = BetRecommendation(
        game_id=market.game_id,
        sportsbook=market.sportsbook,
        side="UNDER",
        line=market.line,
        american_odds=market.under_odds,
        decimal_odds=under_dec,
        model_prob=price.prob_under,
        expected_total=price.expected_total,
        ev=under_ev,
        is_positive_ev=under_ev >= min_edge,
    )
    return over_rec, under_rec


def screen_positive_ev(
    evaluations: Sequence[Tuple[TotalLinePrice, MarketTotalLine]],
    *,
    min_edge: float = MIN_EV_EDGE,
    excluded_books: Collection[str] = LOW_LIQUIDITY_BOOKS,
) -> List[BetRecommendation]:
    """All +EV recommendations from eligible books, sorted by ev DESC."""
    excluded = {b.lower() for b in excluded_books}
    hits: List[BetRecommendation] = []
    for price, market in evaluations:
        if market.sportsbook.lower() in excluded:
            continue
        over_rec, under_rec = evaluate_total_market(price, market, min_edge=min_edge)
        if over_rec.is_positive_ev:
            hits.append(over_rec)
        if under_rec.is_positive_ev:
            hits.append(under_rec)
    hits.sort(key=lambda r: r.ev, reverse=True)
    return hits
