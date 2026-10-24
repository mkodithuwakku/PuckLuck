"""Tests for the bivariate Poisson total-goals model (betting/poisson.py)."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import pytest

import db
from betting.poisson import (
    TeamMatchupInputs,
    build_matchup_inputs_from_db,
    compute_expected_goals,
    goalie_adjustment,
    poisson_pmf_grid,
    price_total_goals,
    rest_factor,
)


def _skater_row(
    game_id: int,
    player_id: int,
    team_id: int,
    opp_id: int,
    game_date: str,
    *,
    ixg: float,
    toi: int = 1800,
) -> Dict[str, Any]:
    return {
        "game_id": game_id,
        "player_id": player_id,
        "season": 2026,
        "game_date": game_date,
        "team_id": team_id,
        "opponent_id": opp_id,
        "is_home": 1,
        "rest_days": 2,
        "toi_seconds": toi,
        "pp_toi_seconds": 0,
        "goals": 0,
        "assists": 0,
        "shots_on_goal": 0,
        "blocks": 0,
        "hits": 0,
        "faceoffs_won": 0,
        "pim": 0,
        "ixg": ixg,
        "icorsi": 0,
        "ihdcf": 0,
        "on_ice_xg_share": 0.0,
    }


def _goalie_row(
    game_id: int,
    goalie_id: int,
    team_id: int,
    opp_id: int,
    game_date: str,
    *,
    gsax: float,
    toi: int = 3600,
) -> Dict[str, Any]:
    return {
        "game_id": game_id,
        "goalie_id": goalie_id,
        "season": 2026,
        "game_date": game_date,
        "team_id": team_id,
        "opponent_id": opp_id,
        "is_home": 0,
        "decision": "W",
        "toi_seconds": toi,
        "shots_against": 30,
        "goals_against": 2,
        "saves": 28,
        "gsax": gsax,
        "hd_save_pct": None,
    }


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = db.init_db(":memory:")
    db.upsert_team(c, 25, "EDM", "Edmonton Oilers", "Rogers Place")
    db.upsert_team(c, 21, "COL", "Colorado Avalanche", "Ball Arena")
    for pid in (1001, 1002, 2001, 2002):
        db.upsert_player(c, pid, f"Player {pid}", "C")
    db.upsert_player(c, 901, "Away Goalie", "G")
    c.commit()
    yield c
    c.close()


def test_goalie_adjustment_and_unconfirmed_backup_penalty() -> None:
    # SRS §5.1: 1 - GSAx/60 / 3.0, clamped to [0.5, 1.5].
    assert goalie_adjustment(1.5) == pytest.approx(0.5)
    assert goalie_adjustment(0.0) == pytest.approx(1.0)
    assert goalie_adjustment(-1.5) == pytest.approx(1.5)
    assert goalie_adjustment(9.0) == pytest.approx(0.5)  # clamp floor
    assert goalie_adjustment(-9.0) == pytest.approx(1.5)  # clamp ceiling

    # rest_days=1 -> rest_factor 1.00 so the multipliers isolate cleanly.
    neutral = TeamMatchupInputs(rest_days=1)
    lam_h, lam_a = compute_expected_goals(neutral, neutral)
    assert lam_h == pytest.approx(3.05 * 1.04)
    assert lam_a == pytest.approx(3.05)

    # Away goalie unconfirmed -> exactly +0.22 on lambda_home.
    away_unconfirmed = TeamMatchupInputs(rest_days=1, opposing_goalie_confirmed=False)
    lam_h2, lam_a2 = compute_expected_goals(away_unconfirmed, neutral)
    assert lam_h2 == pytest.approx(lam_h + 0.22)
    assert lam_a2 == pytest.approx(lam_a)

    # Home goalie unconfirmed -> exactly +0.22 on lambda_away.
    home_unconfirmed = TeamMatchupInputs(rest_days=1, opposing_goalie_confirmed=False)
    lam_h3, lam_a3 = compute_expected_goals(neutral, home_unconfirmed)
    assert lam_h3 == pytest.approx(lam_h)
    assert lam_a3 == pytest.approx(lam_a + 0.22)


def test_rest_factor() -> None:
    assert rest_factor(0) == pytest.approx(0.95)
    assert rest_factor(1) == pytest.approx(1.00)
    assert rest_factor(2) == pytest.approx(1.02)
    assert rest_factor(5) == pytest.approx(1.02)


def test_price_total_goals_grid_properties() -> None:
    grid = poisson_pmf_grid(3.2, 3.0)
    assert grid.shape == (16, 16)
    # Truncated at 15 goals/team, so tail mass >15 is excluded.
    assert grid.sum() == pytest.approx(1.0, abs=1e-4)

    price = price_total_goals(3.2, 3.0, 6.5)
    assert price.prob_push == 0.0  # half-goal line can never push
    assert price.prob_over + price.prob_under == pytest.approx(1.0, abs=1e-4)
    assert price.expected_total == pytest.approx(6.2)

    price_int = price_total_goals(3.2, 3.0, 6.0)
    assert price_int.prob_push > 0.0
    total = price_int.prob_over + price_int.prob_under + price_int.prob_push
    assert total == pytest.approx(1.0, abs=1e-4)

    # Higher combined intensity strictly increases P(Over 6.5).
    hotter = price_total_goals(4.5, 4.0, 6.5)
    assert hotter.prob_over > price.prob_over


def test_build_matchup_inputs_from_db_temporal_integrity(
    conn: sqlite3.Connection,
) -> None:
    """SRS §8: rows >= as_of_date cannot move any matchup input."""
    # Two pre-as_of games: team 25 generates 4.0 xGF/game, team 21 gets 2.0.
    for gid, day in ((1, "2026-10-18"), (2, "2026-10-20")):
        for pid in (1001, 1002):
            db.insert_skater_game(conn, _skater_row(gid, pid, 25, 21, day, ixg=2.0))
        for pid in (2001, 2002):
            db.insert_skater_game(conn, _skater_row(gid, pid, 21, 25, day, ixg=1.0))
        db.insert_goalie_game(conn, _goalie_row(gid, 901, 21, 25, day, gsax=1.0))
    conn.commit()

    home_before, away_before = build_matchup_inputs_from_db(
        conn, 25, 21, "2026-10-23", away_goalie_id=901
    )

    # League mean per-game xg = mean(4, 2, 4, 2) = 3.0.
    assert home_before.team_offense == pytest.approx(4.0 / 3.0)
    assert home_before.opponent_defense == pytest.approx(4.0 / 3.0)
    assert away_before.team_offense == pytest.approx(2.0 / 3.0)
    assert away_before.opponent_defense == pytest.approx(2.0 / 3.0)
    # Both teams last played 10-20 -> rest = 3 - 1 = 2.
    assert home_before.rest_days == 2
    assert away_before.rest_days == 2
    # Away goalie (team 21, id 901): gsax 1.0 per 3600s -> ewma 1.0.
    assert home_before.opposing_goalie_gsax_per60 == pytest.approx(1.0)
    # Home goalie (team 25) has no history -> 0.0.
    assert away_before.opposing_goalie_gsax_per60 == 0.0

    # Future outlier rows on the as_of date itself must be invisible.
    db.insert_skater_game(conn, _skater_row(900, 1001, 25, 21, "2026-10-23", ixg=20.0))
    db.insert_goalie_game(conn, _goalie_row(900, 901, 21, 25, "2026-10-23", gsax=-9.0))
    conn.commit()

    home_after, away_after = build_matchup_inputs_from_db(
        conn, 25, 21, "2026-10-23", away_goalie_id=901
    )
    assert home_after == home_before
    assert away_after == away_before


def test_goalie_auto_lookup_uses_most_recent(conn: sqlite3.Connection) -> None:
    """No explicit goalie_id -> most recent team goalie before as_of_date."""
    db.upsert_player(conn, 902, "Backup Goalie", "G")
    for gid, day, gsax in (
        (1, "2026-10-15", 3.0),
        (2, "2026-10-20", -1.0),
    ):
        db.insert_goalie_game(conn, _goalie_row(gid, 901, 21, 25, day, gsax=gsax))
    conn.commit()

    home, _ = build_matchup_inputs_from_db(conn, 25, 21, "2026-10-23")
    # EWMA of [3.0, -1.0] at alpha 0.08 = 0.08*-1 + 0.92*3.0 = 2.68.
    assert home.opposing_goalie_gsax_per60 == pytest.approx(2.68)
