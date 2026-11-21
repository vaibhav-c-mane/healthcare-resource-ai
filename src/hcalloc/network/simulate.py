"""SYNTHETIC network of hospitals with controllable surges. Nothing here describes a real hospital.

Daily bed demand per hospital = seasonal level x (1 + AR(1) noise) + surge episodes.
The noise has a common part shared by all hospitals (``rho``) and a local part.

Surge regimes in the final (test) year:
    baseline       two random local surges per year, as in every earlier year
    local          four surges, each at ONE hospital (rotating)
    simultaneous   the same four surges at EVERY hospital on the same days
    near_capacity  baseline surges, but every hospital runs at about 95% of its beds all year

Every year before the test year has two random local surges, so forecasters and
scenario calibration have seen surges before. All parameter values are assumptions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..data.series import DailySeries

REGIMES = ("baseline", "local", "simultaneous", "near_capacity")


@dataclass(frozen=True)
class NetworkSimSpec:
    names: tuple[str, ...] = ("Hospital A", "Hospital B", "Hospital C")
    beds: tuple[int, ...] = (150, 250, 400)
    occupancy: float = 0.85  # mean demand as a share of beds
    winter_amplitude: float = 0.06  # relative change at the mid-January peak
    weekend_dip: float = 0.03
    noise_sd: float = 0.03  # stationary relative sd of the AR(1) noise
    ar: float = 0.8
    rho: float = 0.5  # share of noise variance common to all hospitals
    surge_size: float = 0.20  # peak extra demand as a share of the level
    surge_days: int = 21
    surges_per_year: int = 2
    test_surges: int = 4
    years: int = 5
    start: str = "2020-01-01"


def _bump(n_days: int, start: int, length: int, size: float) -> np.ndarray:
    """Raised-cosine bump of height ``size`` over ``length`` days."""
    out = np.zeros(n_days)
    t = np.arange(length)
    shape = 0.5 * (1 - np.cos(2 * np.pi * (t + 0.5) / length))
    end = min(n_days, start + length)
    out[start:end] = size * shape[: end - start]
    return out


def simulate_network(regime: str = "baseline", seed: int = 0, spec: NetworkSimSpec = NetworkSimSpec()) -> tuple[DailySeries, np.ndarray]:
    """Returns the series (units = hospitals, census-only) and a (days, N) mask of days inside a surge."""
    if regime not in REGIMES:
        raise ValueError(f"unknown regime {regime!r}; choose from {REGIMES}")
    rng = np.random.default_rng([seed, REGIMES.index(regime)])
    hist_rng = np.random.default_rng([seed, 99])  # history is the same in every regime for one seed
    dates = pd.date_range(spec.start, periods=int(round(spec.years * 365.25)), freq="D")
    dates = dates[dates.year < pd.Timestamp(spec.start).year + spec.years]
    T, N = len(dates), len(spec.names)
    beds = np.asarray(spec.beds, dtype=float)
    occ = 0.95 if regime == "near_capacity" else spec.occupancy

    doy = dates.dayofyear.to_numpy()
    season = 1 + spec.winter_amplitude * np.cos(2 * np.pi * (doy - 15) / 365.25)
    weekend = 1 - spec.weekend_dip * (dates.dayofweek.to_numpy() >= 5)
    level = occ * beds[None] * (season * weekend)[:, None]

    innov_sd = spec.noise_sd * np.sqrt(1 - spec.ar**2)
    common = hist_rng.normal(0, 1, T)
    local = hist_rng.normal(0, 1, (T, N))
    eps = innov_sd * (np.sqrt(spec.rho) * common[:, None] + np.sqrt(1 - spec.rho) * local)
    z = np.zeros((T, N))
    for t in range(1, T):
        z[t] = spec.ar * z[t - 1] + eps[t]

    surge = np.zeros((T, N))
    years = sorted(set(dates.year))
    test_year = years[-1]
    for y in years:
        idx = np.flatnonzero(dates.year == y)
        if y == test_year and regime in ("local", "simultaneous"):
            starts = np.linspace(idx[0] + 20, idx[-1] - spec.surge_days - 20, spec.test_surges).astype(int)
            starts = starts + rng.integers(-10, 11, len(starts))
            for k, s in enumerate(starts):
                who = range(N) if regime == "simultaneous" else [k % N]
                for n in who:
                    surge[:, n] += _bump(T, s, spec.surge_days, spec.surge_size)
            continue
        r = rng if y == test_year else hist_rng
        for _ in range(spec.surges_per_year):
            s = int(r.integers(idx[0], idx[-1] - spec.surge_days))
            n = int(r.integers(0, N))
            surge[:, n] += _bump(T, s, spec.surge_days, spec.surge_size)

    demand = np.maximum(level * (1 + z + surge), 0.0)
    census = np.round(demand)
    nan = np.full_like(census, np.nan)
    series = DailySeries(
        dates, list(spec.names), census, nan, nan.copy(),
        provenance={"kind": "SYNTHETIC network simulator", "regime": regime, "seed": seed, "spec": asdict(spec)},
    )
    return series, surge > 0.02
