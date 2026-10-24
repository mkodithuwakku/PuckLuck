"""Tests for the +EV screener & consensus odds (betting/ev.py, SRS §5.3)."""

from __future__ import annotations

import sqlite3

import pytest

import db
from betting.ev import (
    LOW_LIQUIDITY_BOOKS,
    MarketTotalLine,
    american_to_decimal,
    compute_ev,
    consensus_total_line,
    evaluate_total_market,
    screen_positive_ev,
    upsert_total_line,
)
from betting.poisson import TotalLinePrice


def _price(prob_over: float) -> TotalLinePrice:
    return TotalLinePrice(
        line=6.5,
        prob_over=prob_over,
        prob_under=1.0 - prob_over,
        prob_push=0.0,
        lambda_home=3.2,
        lambda_away=3.0,
    )


def _market(
    book: str, over: int = -110, under: int = -110, line: float = 6.5
) -> MarketTotalLine:
    return MarketTotalLine(
        game_id=1,
        sportsbook=book,
        line=line,
        over_odds=over,
        under_odds=under,
        captured_at="2026-10-23T12:00:00",
    )


def test_american_to_decimal_and_ev_formula() -> None:
    assert american_to_decimal(-110) == pytest.approx(1.9090909, abs=1e-6)
    assert american_to_decimal(+120) == pytest.approx(2.20)
    assert american_to_decimal(+100) == pytest.approx(2.00)
    assert american_to_decimal(-250) == pytest.approx(1.40)
    with pytest.raises(ValueError):
        american_to_decimal(50)
    with pytest.raises(ValueError):
        american_to_decimal(-50)
    with pytest.raises(ValueError):
        american_to_decimal(0)

    # EV = p * O - 1 (SRS §5.3).
    assert compute_ev(0.55, 2.00) == pytest.approx(0.10)


def test_ev_gate_enforces_3_5_pct_threshold_and_filters_low_liquidity() -> None:
    # -110 decimal = 1.9091. p = (1 + ev) / 1.9091.
    just_under = _price(0.5415)  # ev ≈ +3.41%
    just_over = _price(0.5430)  # ev ≈ +3.66%
    market = _market("pinnacle")

    over_rec, under_rec = evaluate_total_market(just_under, market)
    assert over_rec.ev == pytest.approx(0.5415 * 1.9090909 - 1.0)
    assert over_rec.is_positive_ev is False
    assert under_rec.is_positive_ev is False

    over_rec2, _ = evaluate_total_market(just_over, market)
    assert over_rec2.ev >= 0.035
    assert over_rec2.is_positive_ev is True

    # Screener: huge edge at a low-liquidity book must be filtered.
    evaluations = [
        (_price(0.80), _market("offshore_low")),  # massive but excluded
        (just_over, _market("pinnacle")),
        (_price(0.60), _market("bet365")),  # ev ≈ +14.5%
    ]
    hits = screen_positive_ev(evaluations)
    assert [r.sportsbook for r in hits] == ["bet365", "pinnacle"]
    assert all(r.ev >= 0.035 for r in hits)
    assert hits[0].ev >= hits[1].ev  # sorted desc
    assert "offshore_low" in LOW_LIQUIDITY_BOOKS


def test_upsert_total_line_dedupes_null_target_id() -> None:
    conn = db.init_db(":memory:")
    upsert_total_line(conn, _market("pinnacle", over=-110, under=-110))
    upsert_total_line(conn, _market("pinnacle", over=-105, under=-115))
    rows = conn.execute(
        "SELECT * FROM fact_market_lines WHERE game_id = 1 AND "
        "sportsbook = 'pinnacle' AND market_type = 'TOTAL_GOALS'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["over_odds"] == -105
    assert rows[0]["under_odds"] == -115
    assert rows[0]["target_id"] is None
    conn.close()


def test_consensus_total_line() -> None:
    lines = [
        MarketTotalLine(1, "pinnacle", 6.5, -110, -110, "2026-10-23T11:00:00"),
        MarketTotalLine(1, "bet365", 6.5, -105, -115, "2026-10-23T12:30:00"),
        MarketTotalLine(1, "draftkings", 7.0, 100, -120, "2026-10-23T12:00:00"),
        # Low-liquidity outlier that must not pull the consensus.
        MarketTotalLine(1, "Offshore_Low", 5.5, 150, -200, "2026-10-23T13:00:00"),
    ]
    consensus = consensus_total_line(lines)
    assert consensus is not None
    assert consensus.sportsbook == "consensus"
    assert consensus.line == pytest.approx(6.5)  # median of eligible
    # Best price among books actually dealing 6.5 (pinnacle + bet365).
    assert consensus.over_odds == -105  # max(-110, -105)
    assert consensus.under_odds == -110  # max(-110, -115)
    assert consensus.captured_at == "2026-10-23T12:30:00"  # max eligible

    # Exclusion is case-insensitive; all-low-liquidity -> None.
    assert (
        consensus_total_line([MarketTotalLine(1, "SOCIAL_BOOK", 6.5, -110, -110, "t")])
        is None
    )
