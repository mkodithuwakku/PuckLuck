"""Closed-form Ridge component regressions (SRS §4.1).

The model never predicts aggregate fantasy points — it predicts the
underlying statistical components (SOG, iXG, G, A, BLK, HIT) via
independent ridge solves ``beta = (X^T X + λI)^{-1} X^T y``, then the
``ScoringRescorer`` maps component predictions through the league's
scoring weights. Predictions are clamped nonnegative.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Sequence, Tuple, Union

import numpy as np

COMPONENTS: Tuple[str, ...] = ("SOG", "iXG", "G", "A", "BLK", "HIT")


class RidgeRegressor:
    """Single-target ridge: ``(X^T X + λI)^{-1} X^T y`` via np.linalg.solve."""

    def __init__(self, l2_penalty: float = 1.0, fit_intercept: bool = True) -> None:
        if l2_penalty <= 0:
            raise ValueError("l2_penalty must be > 0")
        self.l2_penalty = float(l2_penalty)
        self.fit_intercept = bool(fit_intercept)
        self.coef_: Optional[np.ndarray] = None
        self.intercept_: float = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray) -> "RidgeRegressor":
        X = np.asarray(X, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if X.ndim != 2:
            raise ValueError("X must be a 2D matrix")
        if X.shape[0] == 0:
            raise ValueError("cannot fit ridge on 0 samples")
        if y.shape[0] != X.shape[0]:
            raise ValueError("X and y have mismatched sample counts")

        n_features = X.shape[1]
        penalty = self.l2_penalty * np.eye(n_features)
        if self.fit_intercept:
            x_mean = X.mean(axis=0)
            y_mean = float(y.mean())
            x_c = X - x_mean
            y_c = y - y_mean
            beta = np.linalg.solve(x_c.T @ x_c + penalty, x_c.T @ y_c)
            self.intercept_ = float(y_mean - x_mean @ beta)
        else:
            beta = np.linalg.solve(X.T @ X + penalty, X.T @ y)
            self.intercept_ = 0.0
        self.coef_ = beta
        return self

    def predict(
        self, X: Union[np.ndarray, Sequence[float]], *, clamp_nonnegative: bool = True
    ) -> np.ndarray:
        if self.coef_ is None:
            raise ValueError("model is not fitted")
        X = np.asarray(X, dtype=np.float64)
        raw = X @ self.coef_ + self.intercept_
        return np.maximum(0.0, raw) if clamp_nonnegative else raw


class ComponentRidgeModel:
    """One RidgeRegressor per fantasy component (SOG, iXG, G, A, BLK, HIT)."""

    def __init__(
        self,
        *,
        l2_penalty: float = 1.0,
        fit_intercept: bool = True,
        components: Sequence[str] = COMPONENTS,
    ) -> None:
        self._l2_penalty = float(l2_penalty)
        self._fit_intercept = bool(fit_intercept)
        self._components = tuple(components)
        self._models: Dict[str, RidgeRegressor] = {}

    @property
    def components(self) -> Tuple[str, ...]:
        return self._components

    def fit(
        self, X: np.ndarray, y_by_component: Dict[str, np.ndarray]
    ) -> "ComponentRidgeModel":
        for comp in self._components:
            model = RidgeRegressor(self._l2_penalty, self._fit_intercept)
            model.fit(X, y_by_component[comp])
            self._models[comp] = model
        return self

    def predict_components(
        self, x: Union[np.ndarray, Sequence[float]]
    ) -> Dict[str, float]:
        """Clamped nonnegative component predictions for one 1D vector."""
        vec = np.asarray(x, dtype=np.float64).reshape(1, -1)
        return {
            comp: float(model.predict(vec)[0]) for comp, model in self._models.items()
        }

    def predict_batch(self, X: np.ndarray) -> Dict[str, np.ndarray]:
        """Clamped nonnegative component predictions for a 2D matrix."""
        return {comp: model.predict(X) for comp, model in self._models.items()}


BonusFn = Callable[[Dict[str, float]], float]


class ScoringRescorer:
    """Maps predicted components into league fantasy points (SRS §4.1)."""

    def __init__(
        self,
        stat_weights: Dict[str, float],
        *,
        bonus_fn: Optional[BonusFn] = None,
    ) -> None:
        self._weights = {k: float(v) for k, v in stat_weights.items()}
        self._bonus_fn = bonus_fn

    def score(self, predicted_components: Dict[str, float]) -> float:
        """``sum_s (s_hat * weight_s) + bonus_fn(predicted_components)``."""
        base = sum(
            predicted_components.get(stat, 0.0) * weight
            for stat, weight in self._weights.items()
        )
        bonus = self._bonus_fn(predicted_components) if self._bonus_fn else 0.0
        return float(base + bonus)
