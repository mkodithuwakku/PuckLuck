"""Tests for the H-week add/drop gate (fantasy/solver.py, SRS §4.2/§8)."""

from __future__ import annotations

import pytest

from fantasy.solver import (
    ACTION_HOLD_ASSET,
    ACTION_RECOMMEND_SWAP,
    CandidatePlayer,
    evaluate_swap,
    solve_best_stream,
)


def test_elite_asset_hold_enforced_with_exact_srs_message() -> None:
    """SRS §8 gate: HOLD_ASSET must emit the exact §4.2 wording."""
    streamer = CandidatePlayer(
        player_id=9001,
        display_name="Streamer Winger",
        fp_tonight=4.2,
        fp_remaining_week=(2.0,),
        fp_ros_weekly_avg=10.0,
    )
    drop = CandidatePlayer(
        player_id=8480803,
        display_name="Evan Bouchard",
        position="D",
        fp_tonight=0.0,  # off tonight
        fp_remaining_week=(3.0,),
        fp_ros_weekly_avg=14.0,
    )
    ev = evaluate_swap(streamer, drop, horizon_weeks=2.5)

    # short_term = 6.2 - 3.0 = +3.2; penalty = 2.5*(10-14) = -10.0.
    assert ev.short_term_gain == pytest.approx(3.2)
    assert ev.asset_depreciation_penalty == pytest.approx(-10.0)
    assert ev.delta_value == pytest.approx(-6.8)
    assert ev.action == ACTION_HOLD_ASSET
    assert ev.explanation == (
        "Short-term streaming gain of +4.2 FP tonight is rejected: "
        "dropping Evan Bouchard results in net loss of -6.8 FP over "
        "the next 2.5 weeks."
    )


def test_positive_delta_emits_recommend_swap() -> None:
    streamer = CandidatePlayer(
        player_id=9002,
        display_name="Hot Streamer",
        fp_tonight=5.0,
        fp_remaining_week=(6.0,),
        fp_ros_weekly_avg=12.0,
    )
    drop = CandidatePlayer(
        player_id=9003,
        display_name="Cold Roster Spot",
        fp_tonight=1.0,
        fp_remaining_week=(2.0,),
        fp_ros_weekly_avg=8.0,
    )
    ev = evaluate_swap(streamer, drop, horizon_weeks=2.5)

    # short = 11 - 3 = 8; penalty = 2.5*(12-8) = 10; delta = 18.
    assert ev.delta_value == pytest.approx(18.0)
    assert ev.action == ACTION_RECOMMEND_SWAP
    assert ev.tonight_gain == pytest.approx(4.0)
    assert ev.explanation == (
        "Recommended swap: adding Hot Streamer for Cold Roster Spot "
        "yields +18.0 net FP over the next 2.5 weeks."
    )


def test_solve_best_stream_skips_undroppable_and_ranks_highest_delta() -> None:
    undroppable_ice_cold = CandidatePlayer(
        player_id=1,
        display_name="Protected Elite",
        fp_tonight=0.0,
        fp_remaining_week=(0.0,),
        fp_ros_weekly_avg=20.0,
        is_droppable=False,  # huge weekly cost if it *could* be dropped
    )
    droppable_weak = CandidatePlayer(
        player_id=2,
        display_name="Weak Droppable",
        fp_tonight=0.5,
        fp_remaining_week=(1.0,),
        fp_ros_weekly_avg=6.0,
    )
    droppable_mid = CandidatePlayer(
        player_id=3,
        display_name="Mid Droppable",
        fp_tonight=1.5,
        fp_remaining_week=(2.0,),
        fp_ros_weekly_avg=9.0,
    )
    streamer_a = CandidatePlayer(
        player_id=10,
        display_name="Streamer A",
        fp_tonight=3.0,
        fp_remaining_week=(4.0,),
        fp_ros_weekly_avg=10.0,
    )
    streamer_b = CandidatePlayer(
        player_id=11,
        display_name="Streamer B",
        fp_tonight=4.0,
        fp_remaining_week=(5.0,),
        fp_ros_weekly_avg=11.0,
    )

    best = solve_best_stream(
        [streamer_a, streamer_b],
        [undroppable_ice_cold, droppable_weak, droppable_mid],
        horizon_weeks=2.5,
    )
    assert best is not None
    # The undroppable player must never appear as the drop target.
    assert best.drop_candidate.is_droppable
    # Best pair = streamer B vs weakest droppable asset.
    assert best.streamer.player_id == 11
    assert best.drop_candidate.player_id == 2
    expected_delta = (9.0 - 1.5) + 2.5 * (11.0 - 6.0)
    assert best.delta_value == pytest.approx(expected_delta)

    # Verify it really is the max over all legal pairs.
    for s in (streamer_a, streamer_b):
        for d in (droppable_weak, droppable_mid):
            ev = evaluate_swap(s, d, horizon_weeks=2.5)
            assert ev.delta_value <= best.delta_value + 1e-12


def test_solve_best_stream_empty_inputs() -> None:
    droppable = CandidatePlayer(player_id=2, display_name="Weak", is_droppable=True)
    assert solve_best_stream([], [droppable]) is None
    undroppable = CandidatePlayer(player_id=1, display_name="Elite", is_droppable=False)
    assert solve_best_stream([droppable], [undroppable]) is None
    assert solve_best_stream([], []) is None


def test_evaluate_swap_rejects_invalid_inputs() -> None:
    streamer = CandidatePlayer(player_id=1, display_name="S", fp_tonight=1.0)
    not_droppable = CandidatePlayer(
        player_id=2, display_name="Protected", is_droppable=False
    )
    with pytest.raises(ValueError):
        evaluate_swap(streamer, not_droppable)
    droppable = CandidatePlayer(player_id=3, display_name="OK")
    with pytest.raises(ValueError):
        evaluate_swap(streamer, droppable, horizon_weeks=-1.0)
