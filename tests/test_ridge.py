"""Tests for closed-form Ridge regression (models/ridge.py, SRS §4.1/§8)."""

from __future__ import annotations

import numpy as np
import pytest

from models.ridge import (
    COMPONENTS,
    ComponentRidgeModel,
    RidgeRegressor,
    ScoringRescorer,
)


def test_closed_form_ridge_matches_reference_solution() -> None:
    """SRS §8 gate: must match the normal equations within 1e-5."""
    rng = np.random.default_rng(42)
    X = rng.normal(size=(60, 10))
    beta_true = rng.uniform(-2.0, 3.0, size=10)
    y = X @ beta_true + 1.7 + rng.normal(0.0, 0.1, size=60)
    lam = 1.5

    # fit_intercept=True: centered normal equations.
    reg = RidgeRegressor(l2_penalty=lam, fit_intercept=True).fit(X, y)
    x_mean = X.mean(axis=0)
    y_mean = y.mean()
    xc = X - x_mean
    yc = y - y_mean
    beta_ref = np.linalg.solve(xc.T @ xc + lam * np.eye(10), xc.T @ yc)
    intercept_ref = float(y_mean - x_mean @ beta_ref)
    assert np.allclose(reg.coef_, beta_ref, atol=1e-5)
    assert reg.intercept_ == pytest.approx(intercept_ref, abs=1e-10)

    # fit_intercept=False: uncentered normal equations.
    reg_ni = RidgeRegressor(l2_penalty=lam, fit_intercept=False).fit(X, y)
    beta_ref_ni = np.linalg.solve(X.T @ X + lam * np.eye(10), X.T @ y)
    assert np.allclose(reg_ni.coef_, beta_ref_ni, atol=1e-10)
    assert reg_ni.intercept_ == 0.0


def test_predictions_clamped_nonnegative() -> None:
    X = np.array([[1.0], [2.0], [3.0]])
    y = np.array([1.0, 2.0, 3.0])
    reg = RidgeRegressor(l2_penalty=0.5).fit(X, y)

    neg_input = np.array([[-100.0]])
    raw = neg_input @ reg.coef_ + reg.intercept_
    assert raw[0] < 0  # proves the clamp is doing real work
    assert reg.predict(neg_input)[0] == 0.0
    assert reg.predict(neg_input, clamp_nonnegative=False)[0] == pytest.approx(raw[0])


def test_ridge_validation() -> None:
    with pytest.raises(ValueError):
        RidgeRegressor(l2_penalty=0.0)
    with pytest.raises(ValueError):
        RidgeRegressor(l2_penalty=-1.0)
    with pytest.raises(ValueError):
        RidgeRegressor(1.0).fit(np.empty((0, 3)), np.empty((0,)))


def test_component_ridge_and_scoring_rescorer() -> None:
    """SRS §4.1: predict components first; rescoring is a separate map."""
    rng = np.random.default_rng(7)
    X = rng.normal(size=(80, 10))
    y_by_comp = {c: np.abs(X @ rng.uniform(0.1, 1.0, size=10)) for c in COMPONENTS}
    model = ComponentRidgeModel(l2_penalty=1.0).fit(X, y_by_comp)

    x = X[0]
    comps = model.predict_components(x)
    assert set(comps) == set(COMPONENTS)
    assert all(v >= 0.0 for v in comps.values())

    batch = model.predict_batch(X[:5])
    assert set(batch) == set(COMPONENTS)
    assert batch["G"].shape == (5,)
    assert np.all(batch["G"] >= 0.0)

    # Exact weighted sum + bonus on a hand-specified component dict.
    rescorer = ScoringRescorer(
        {"G": 3.0, "A": 2.0, "SOG": 0.4},
        bonus_fn=lambda c: 0.5 if c.get("G", 0.0) >= 1.0 else 0.0,
    )
    predicted = {"G": 1.0, "A": 2.0, "SOG": 5.0}
    assert rescorer.score(predicted) == pytest.approx(3.0 + 4.0 + 2.0 + 0.5)

    no_bonus = ScoringRescorer({"G": 3.0})
    assert no_bonus.score(predicted) == pytest.approx(3.0)
