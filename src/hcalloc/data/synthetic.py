"""Semi-synthetic hospital simulator.

SYNTHETIC DATA. Nothing produced here describes a real hospital. The simulator
generates patient-level stays internally (arrivals and lengths of stay per
unit) and exports only daily aggregates, mirroring the privacy-by-design rule
that the planning system never sees patient records.

Structure follows patterns reported for real inpatient flow: weekday elective
surgery, winter peaks and respiratory waves for medical and ICU demand, fewer
weekend discharges, and lognormal lengths of stay. Parameter values are
assumptions chosen to give plausible occupancy for the default 4-unit hospital;
they are listed in ``docs/assumptions.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

from .calendar import bank_holidays
from .series import DailySeries


@dataclass(frozen=True)
class UnitFlowSpec:
    name: str
    admissions_per_day: float
    weekday_profile: tuple[float, ...]  # Monday..Sunday, relative
    winter_amplitude: float  # relative change at the mid-January peak
    respiratory_sensitivity: float  # response to respiratory waves (negative = electives cancelled)
    holiday_factor: float  # admissions multiplier on public holidays
    los_mean: float  # days
    los_sdlog: float  # lognormal shape
    weekend_discharge_delay: float  # probability a Sat/Sun discharge slips to Monday
    stress_sensitivity: float  # response to a pandemic-like stress wave


DEFAULT_FLOWS: tuple[UnitFlowSpec, ...] = (
    UnitFlowSpec("ICU", 4.0, (1.05, 1.05, 1.0, 1.0, 1.0, 0.95, 0.95), 0.10, 1.0, 0.95, 3.5, 0.8, 0.2, 1.0),
    UnitFlowSpec("StepDown", 6.0, (1.05, 1.05, 1.05, 1.0, 1.0, 0.9, 0.95), 0.08, 0.6, 0.95, 3.0, 0.7, 0.4, 0.6),
    UnitFlowSpec("Medical", 11.0, (1.15, 1.05, 1.0, 1.0, 1.0, 0.85, 0.95), 0.15, 1.0, 1.05, 5.0, 0.8, 0.5, 0.8),
    UnitFlowSpec("Surgical", 9.5, (1.3, 1.3, 1.25, 1.25, 1.2, 0.35, 0.35), -0.03, -0.3, 0.4, 4.0, 0.7, 0.3, -0.5),
)


@dataclass(frozen=True)
class StressWave:
    """A pandemic-like demand wave, used as a held-out robustness test."""

    start: str
    days: int = 70
    peak: float = 0.5


def _respiratory_waves(dates: pd.DatetimeIndex, rng: np.random.Generator) -> np.ndarray:
    t = (dates - dates[0]).days.to_numpy().astype(float)
    wave = np.zeros(len(dates))
    for y in range(dates.year.min() - 1, dates.year.max() + 1):
        center = (pd.Timestamp(f"{y}-12-01") - dates[0]).days + rng.uniform(0, 75)
        width = rng.uniform(10, 25)
        amp = rng.uniform(0.05, 0.35)
        wave += amp * np.exp(-0.5 * ((t - center) / width) ** 2)
    return wave


def _stress_curve(dates: pd.DatetimeIndex, stress: StressWave | None) -> np.ndarray:
    curve = np.zeros(len(dates))
    if stress is None:
        return curve
    start = pd.Timestamp(stress.start)
    x = (dates - start).days.to_numpy() / stress.days
    inside = (x >= 0) & (x <= 1)
    curve[inside] = stress.peak * np.sin(np.pi * x[inside]) ** 2
    return curve


def simulate_hospital(
    start: str = "2019-01-01",
    years: int = 5,
    seed: int = 0,
    flows: tuple[UnitFlowSpec, ...] = DEFAULT_FLOWS,
    trend_per_year: float = 0.01,
    stress: StressWave | None = None,
    warmup_days: int = 120,
) -> DailySeries:
    # Independent streams per component, so a stress wave changes nothing before it starts.
    root = np.random.SeedSequence(seed)
    wave_seq, *unit_seqs = root.spawn(1 + len(flows))
    wave_rng = np.random.default_rng(wave_seq)
    first = pd.Timestamp(start)
    last = first + pd.DateOffset(years=years) - pd.Timedelta(days=1)
    dates = pd.date_range(first - pd.Timedelta(days=warmup_days), last, freq="D")
    n_days = len(dates)
    dow = dates.dayofweek.to_numpy()
    doy = dates.dayofyear.to_numpy()
    holidays = np.isin(dates, list(bank_holidays(range(dates.year.min(), dates.year.max() + 1))))
    years_elapsed = (dates - first).days.to_numpy() / 365.25
    waves = _respiratory_waves(dates, wave_rng)
    stress_curve = _stress_curve(dates, stress)

    census = np.zeros((n_days, len(flows)))
    admissions = np.zeros_like(census)
    discharges = np.zeros_like(census)
    for j, f in enumerate(flows):
        noise_rng, count_rng, los_rng, slip_rng = [np.random.default_rng(c) for c in unit_seqs[j].spawn(4)]
        profile = np.asarray(f.weekday_profile) / np.mean(f.weekday_profile)
        rate = (
            f.admissions_per_day
            * profile[dow]
            * (1 + f.winter_amplitude * np.cos(2 * np.pi * (doy - 15) / 365.25))
            * (1 + f.respiratory_sensitivity * waves)
            * np.where(holidays, f.holiday_factor, 1.0)
            * (1 + trend_per_year * years_elapsed)
            * (1 + f.stress_sensitivity * stress_curve)
            * noise_rng.gamma(40.0, 1 / 40.0, n_days)
        )
        counts = count_rng.poisson(np.maximum(rate, 0.05))
        admit_day = np.repeat(np.arange(n_days), counts)
        mu = np.log(f.los_mean) - f.los_sdlog**2 / 2
        los = np.maximum(1, np.rint(np.exp(mu + f.los_sdlog * norm.ppf(los_rng.random(admit_day.size))))).astype(int)
        leave = admit_day + los
        leave_dow = (dow[0] + leave) % 7
        slip = (leave_dow >= 5) & (slip_rng.random(leave.size) < f.weekend_discharge_delay)
        leave = leave + np.where(slip, 7 - leave_dow, 0)
        delta = np.bincount(admit_day, minlength=n_days).astype(float)
        inside = leave < n_days
        out = np.bincount(leave[inside], minlength=n_days).astype(float)
        census[:, j] = np.cumsum(delta - out)
        admissions[:, j] = delta
        discharges[:, j] = out

    keep = slice(warmup_days, None)
    return DailySeries(
        dates=dates[keep],
        units=[f.name for f in flows],
        census=census[keep],
        admissions=admissions[keep],
        discharges=discharges[keep],
        provenance={
            "kind": "synthetic",
            "generator": "hcalloc.data.synthetic.simulate_hospital",
            "seed": seed,
            "start": start,
            "years": years,
            "trend_per_year": trend_per_year,
            "stress": asdict(stress) if stress else None,
            "flows": [asdict(f) for f in flows],
        },
    )
