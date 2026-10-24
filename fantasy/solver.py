"""The Irreversible Transaction Solver (SRS §4.2).

Gates every add/drop through the Net Expected Value test:

    delta = (streamer_weekly_fp - drop_weekly_fp)
          + H * (streamer_ros_avg - drop_ros_avg)

A swap is recommended iff delta > 0. Otherwise the interface MUST emit
``HOLD_ASSET`` with the exact SRS §4.2 rejection wording — dropping an
elite asset for a one-night stream is a permanent loss even when the
short-term gain is positive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

DEFAULT_HORIZON_WEEKS: float = 2.5
ACTION_RECOMMEND_SWAP = "RECOMMEND_SWAP"
ACTION_HOLD_ASSET = "HOLD_ASSET"


@dataclass(frozen=True)
class CandidatePlayer:
    player_id: int
    display_name: str
    position: str = "C"
    yahoo_player_id: Optional[str] = None
    fp_tonight: float = 0.0
    fp_remaining_week: Tuple[float, ...] = ()
    fp_ros_weekly_avg: float = 0.0
    is_droppable: bool = True

    @property
    def total_weekly_fp(self) -> float:
        return float(self.fp_tonight) + float(sum(self.fp_remaining_week))


@dataclass(frozen=True)
class SwapEvaluation:
    streamer: CandidatePlayer
    drop_candidate: CandidatePlayer
    horizon_weeks: float
    tonight_gain: float
    streamer_weekly_fp: float
    drop_weekly_fp: float
    short_term_gain: float
    asset_depreciation_penalty: float
    delta_value: float
    action: str  # "RECOMMEND_SWAP" or "HOLD_ASSET"
    explanation: str


def evaluate_swap(
    streamer: CandidatePlayer,
    drop_candidate: CandidatePlayer,
    *,
    horizon_weeks: float = DEFAULT_HORIZON_WEEKS,
) -> SwapEvaluation:
    """Score one (add, drop) pair against the SRS §4.2 delta gate."""
    if horizon_weeks < 0:
        raise ValueError("horizon_weeks must be >= 0")
    if not drop_candidate.is_droppable:
        raise ValueError(
            f"{drop_candidate.display_name} is not droppable (protected asset)"
        )

    tonight_gain = float(streamer.fp_tonight) - float(drop_candidate.fp_tonight)
    short_term_gain = streamer.total_weekly_fp - drop_candidate.total_weekly_fp
    penalty = horizon_weeks * (
        streamer.fp_ros_weekly_avg - drop_candidate.fp_ros_weekly_avg
    )
    delta = short_term_gain + penalty

    if delta > 0.0:
        action = ACTION_RECOMMEND_SWAP
        explanation = (
            f"Recommended swap: adding {streamer.display_name} for "
            f"{drop_candidate.display_name} yields +{delta:.1f} net FP over "
            f"the next {horizon_weeks:g} weeks."
        )
    else:
        action = ACTION_HOLD_ASSET
        explanation = (
            f"Short-term streaming gain of +{tonight_gain:.1f} FP tonight is "
            f"rejected: dropping {drop_candidate.display_name} results in "
            f"net loss of -{abs(delta):.1f} FP over the next "
            f"{horizon_weeks:g} weeks."
        )

    return SwapEvaluation(
        streamer=streamer,
        drop_candidate=drop_candidate,
        horizon_weeks=horizon_weeks,
        tonight_gain=tonight_gain,
        streamer_weekly_fp=streamer.total_weekly_fp,
        drop_weekly_fp=drop_candidate.total_weekly_fp,
        short_term_gain=short_term_gain,
        asset_depreciation_penalty=penalty,
        delta_value=delta,
        action=action,
        explanation=explanation,
    )


def solve_best_stream(
    free_agents: Sequence[CandidatePlayer],
    roster: Sequence[CandidatePlayer],
    *,
    horizon_weeks: float = DEFAULT_HORIZON_WEEKS,
) -> Optional[SwapEvaluation]:
    """Return the (streamer, drop) pair maximizing delta_value.

    Only ``is_droppable`` roster players may be dropped. Ties break on
    higher ``tonight_gain``. Returns ``None`` when either side is empty.
    """
    droppable = [p for p in roster if p.is_droppable]
    if not free_agents or not droppable:
        return None

    best: Optional[SwapEvaluation] = None
    for streamer in free_agents:
        for drop in droppable:
            ev = evaluate_swap(streamer, drop, horizon_weeks=horizon_weeks)
            if (
                best is None
                or ev.delta_value > best.delta_value
                or (
                    ev.delta_value == best.delta_value
                    and ev.tonight_gain > best.tonight_gain
                )
            ):
                best = ev
    return best
