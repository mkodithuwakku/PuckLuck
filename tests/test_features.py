"""Tests for the EWMA feature pipeline (features/builder.py, SRS §4.1)."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

import numpy as np
import pytest

import db
from features.builder import (
    COMPONENTS,
    FEATURE_NAMES,
    FeatureBuilder,
    compute_ewma,
)


def _skater_row(
    game_id: int,
    player_id: int,
    team_id: int,
    opp_id: int,
    game_date: str,
    *,
    toi: int = 1800,
    pp_toi: int = 0,
    goals: int = 0,
    assists: int = 0,
    shots: int = 0,
    blocks: int = 0,
    hits: int = 0,
    ixg: float = 0.3,
    icorsi: int = 5,
    ihdcf: int = 1,
    xg_share: float = 0.5,
    is_home: int = 1,
    rest: int = 2,
) -> Dict[str, Any]:
    return {
        "game_id": game_id,
        "player_id": player_id,
        "season": 2026,
        "game_date": game_date,
        "team_id": team_id,
        "opponent_id": opp_id,
        "is_home": is_home,
        "rest_days": rest,
        "toi_seconds": toi,
        "pp_toi_seconds": pp_toi,
        "goals": goals,
        "assists": assists,
        "shots_on_goal": shots,
        "blocks": blocks,
        "hits": hits,
        "faceoffs_won": 0,
        "pim": 0,
        "ixg": ixg,
        "icorsi": icorsi,
        "ihdcf": ihdcf,
        "on_ice_xg_share": xg_share,
    }


def _goalie_row(
    game_id: int,
    goalie_id: int,
    team_id: int,
    opp_id: int,
    game_date: str,
    *,
    toi: int = 3600,
    gsax: float = 0.0,
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
    db.upsert_team(c, 22, "CGY", "Calgary Flames", "Saddledome")
    db.upsert_player(c, 1001, "Target Skater", "C")
    db.upsert_player(c, 1002, "Teammate Skater", "LW")
    db.upsert_player(c, 901, "Opponent Goalie", "G")
    c.commit()
    yield c
    c.close()


def test_compute_ewma_weights_recent_games_more_heavily() -> None:
    assert compute_ewma([], 0.08) == 0.0
    assert compute_ewma([3.5], 0.08) == pytest.approx(3.5)
    # Manual recursion on [1.0, 2.0, 5.0], alpha=0.08:
    #   m0 = 1.0
    #   m1 = 0.08*2.0 + 0.92*1.0 = 1.08
    #   m2 = 0.08*5.0 + 0.92*1.08 = 1.3936
    assert compute_ewma([1.0, 2.0, 5.0], 0.08) == pytest.approx(1.3936)
    # Recency weighting: over a ~15+ game horizon the v0 anchor decays
    # (0.92^20 ≈ 0.19) and the recent block dominates the estimate.
    long_series = [1.0] * 20 + [5.0] * 20
    assert compute_ewma(long_series, 0.08) > 4.0


def test_temporal_integrity_future_game_cannot_leak(
    conn: sqlite3.Connection,
) -> None:
    """SRS §8 gate: rows >= as_of_date must have ZERO effect."""
    for gid, day in ((1, "2026-10-18"), (2, "2026-10-20"), (3, "2026-10-22")):
        db.insert_skater_game(
            conn, _skater_row(gid, 1001, 25, 21, day, ixg=0.4, icorsi=7)
        )
    conn.commit()

    fb = FeatureBuilder(conn)
    fv_before = fb.build_player_features(
        1001, 25, 21, "2026-10-23", is_home=True, team_rest=0, opp_rest=2
    )
    X_before, y_before, keys_before = fb.build_training_dataset("2026-10-23")

    # Massive outlier games ON and AFTER as_of_date (must be invisible):
    for gid, day in ((998, "2026-10-23"), (999, "2026-10-24")):
        db.insert_skater_game(
            conn,
            _skater_row(gid, 1001, 25, 21, day, goals=10, ixg=9.5, icorsi=40),
        )
    # And a catastrophic opponent-goalie game that would tank gsax/60.
    db.insert_goalie_game(conn, _goalie_row(998, 901, 21, 25, "2026-10-23", gsax=-8.0))
    conn.commit()

    fb2 = FeatureBuilder(conn)
    fv_after = fb2.build_player_features(
        1001, 25, 21, "2026-10-23", is_home=True, team_rest=0, opp_rest=2
    )
    X_after, y_after, keys_after = fb2.build_training_dataset("2026-10-23")

    assert fv_after == fv_before  # bit-for-bit identical feature dict
    assert np.array_equal(X_after, X_before)
    assert keys_after == keys_before
    for comp in COMPONENTS:
        assert np.array_equal(y_after[comp], y_before[comp])

    # Sanity: X is well-formed — two rows (10-18 had zero priors).
    assert X_before.shape == (2, len(FEATURE_NAMES))
    assert keys_before == [(2, 1001), (3, 1001)]


def test_opp_def_rtg_and_goalie_gsax_suppression(
    conn: sqlite3.Connection,
) -> None:
    """Soft opponent (>0 rel rating) vs stingy (<0); rolling goalie GSAx/60."""
    # COL (21) bleeds 6.0 xGA/game; CGY (22) allows 2.0 xGA/game.
    data = [
        (1, "2026-10-01", 21, 3.0),
        (2, "2026-10-03", 21, 3.0),
        (3, "2026-10-05", 22, 1.0),
        (4, "2026-10-07", 22, 1.0),
    ]
    for gid, day, opp, ixg in data:
        for pid in (1001, 1002):
            db.insert_skater_game(conn, _skater_row(gid, pid, 25, opp, day, ixg=ixg))
    # Opponent goalie history for team 21: gsax -1 then +2 over 3600s.
    db.insert_goalie_game(conn, _goalie_row(1, 901, 21, 25, "2026-10-01", gsax=-1.0))
    db.insert_goalie_game(conn, _goalie_row(2, 901, 21, 25, "2026-10-03", gsax=2.0))
    conn.commit()

    fb = FeatureBuilder(conn)
    soft = fb.build_player_features(
        1001, 25, 21, "2026-10-23", is_home=True, team_rest=2, opp_rest=2
    )
    stingy = fb.build_player_features(
        1001, 25, 22, "2026-10-23", is_home=True, team_rest=2, opp_rest=2
    )

    # League mean per-game xGA = (6 + 6 + 2 + 2) / 4 = 4.0.
    assert soft.features["opp_def_rtg_rel"] == pytest.approx(6.0 / 4.0 - 1.0)
    assert soft.features["opp_def_rtg_rel"] > 0
    assert stingy.features["opp_def_rtg_rel"] == pytest.approx(2.0 / 4.0 - 1.0)
    assert stingy.features["opp_def_rtg_rel"] < 0

    # Goalie auto-lookup picks the most recent team-21 goalie (901).
    # Series [-1.0, +2.0] -> ewma = 0.08*2 + 0.92*(-1) = -0.76.
    assert soft.features["opp_goalie_gsax_per60"] == pytest.approx(-0.76)

    # Explicit goalie id is honored; unknown opponent has no goalie rows.
    assert stingy.features["opp_goalie_gsax_per60"] == 0.0


def test_pp_toi_share_and_schedule_flags(conn: sqlite3.Connection) -> None:
    """PP share = pp_toi / max(team_max_pp, team_sum_pp/5); flags honest."""
    db.insert_skater_game(conn, _skater_row(1, 1001, 25, 21, "2026-10-20", pp_toi=120))
    db.insert_skater_game(conn, _skater_row(1, 1002, 25, 21, "2026-10-20", pp_toi=180))
    conn.commit()

    fb = FeatureBuilder(conn)
    fv = fb.build_player_features(
        1001, 25, 21, "2026-10-23", is_home=True, team_rest=0, opp_rest=2
    )
    share = fv.features["pp_toi_share_ewma"]
    assert 0.0 <= share <= 1.0
    # denom = max(180, 300/5) = 180 -> 120/180.
    assert share == pytest.approx(120.0 / 180.0)
    assert fv.features["b2b_flag"] == 1.0  # team_rest == 0
    assert fv.features["rest_diff"] == pytest.approx(0 - 2)
    assert fv.features["home_flag"] == 1.0

    fv2 = fb.build_player_features(
        1002, 25, 21, "2026-10-23", is_home=False, team_rest=1, opp_rest=2
    )
    assert fv2.features["pp_toi_share_ewma"] == pytest.approx(1.0)
    assert fv2.features["b2b_flag"] == 0.0
    assert fv2.features["rest_diff"] == pytest.approx(1 - 2)
    assert fv2.features["home_flag"] == 0.0


def test_empty_history_yields_zero_features(conn: sqlite3.Connection) -> None:
    fb = FeatureBuilder(conn)
    fv = fb.build_player_features(
        1001, 25, 21, "2026-10-23", is_home=True, team_rest=2, opp_rest=2
    )
    assert fv.games_sampled == 0
    assert fv.features["ixg_per60_ewma"] == 0.0
    assert fv.features["opp_def_rtg_rel"] == 0.0
    assert fv.features["opp_goalie_gsax_per60"] == 0.0
    assert fv.to_array().shape == (len(FEATURE_NAMES),)
