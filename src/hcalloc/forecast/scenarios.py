"""Demand scenarios from point forecasts, calibrated with split-conformal residuals.

Each calibration origin contributes one joint residual matrix (U x H): the
error of the forecaster on every unit and horizon at once. Scenarios are the
point forecast plus residual matrices drawn from that pool, so correlation
across units and days is kept. ``scale`` widens or narrows the spread; 1.0 is
the plain conformal setting, other values are tuned by decision regret.
"""

from __future__ import annotations

import numpy as np


class ConformalScenarios:
    def __init__(self, n_scenarios: int = 40, scale: float = 1.0, seed: int = 0):
        self.n_scenarios = n_scenarios
        self.scale = scale
        self.seed = seed
        self.residuals: np.ndarray | None = None

    def calibrate(self, actual: np.ndarray, predicted: np.ndarray) -> "ConformalScenarios":
        self.residuals = np.asarray(actual, dtype=float) - np.asarray(predicted, dtype=float)
        return self

    def sample(self, point: np.ndarray, key: int = 0, n: int | None = None, scale: float | None = None) -> np.ndarray:
        """Scenarios of shape (S, U, H). ``key`` makes draws reproducible per origin."""
        if self.residuals is None:
            raise RuntimeError("calibrate() first")
        n = n or self.n_scenarios
        scale = self.scale if scale is None else scale
        rng = np.random.default_rng([self.seed, int(key)])
        pick = rng.choice(len(self.residuals), size=n, replace=n > len(self.residuals))
        return np.maximum(point[None] + scale * self.residuals[pick], 0.0)


def interval_coverage(scenarios: np.ndarray, actual: np.ndarray, level: float) -> float:
    """Share of actual values inside the central ``level`` interval of the scenarios (axis 1 = scenarios)."""
    lo = np.quantile(scenarios, (1 - level) / 2, axis=1)
    hi = np.quantile(scenarios, 1 - (1 - level) / 2, axis=1)
    return float(np.mean((actual >= lo) & (actual <= hi)))


def crps_ensemble(scenarios: np.ndarray, actual: np.ndarray) -> float:
    """Mean CRPS of ensemble forecasts. scenarios: (K, S, ...), actual: (K, ...)."""
    term1 = np.abs(scenarios - actual[:, None]).mean(axis=1)
    sorted_s = np.sort(scenarios, axis=1)
    s = sorted_s.shape[1]
    weights = (2 * np.arange(1, s + 1) - s - 1).reshape((1, s) + (1,) * (scenarios.ndim - 2))
    term2 = (weights * sorted_s).sum(axis=1) / (s * s)  # = 0.5 * E|X - X'|
    return float((term1 - term2).mean())
