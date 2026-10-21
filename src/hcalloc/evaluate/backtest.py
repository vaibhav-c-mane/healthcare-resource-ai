"""Rolling-origin backtests: forecast, plan, then score each plan on what really happened."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..allocate.baselines import make_plan
from ..allocate.model import cvar, solve_allocation
from ..config import HospitalConfig, PlanSettings
from ..data.series import DailySeries
from ..forecast.models import Forecaster, SeasonalNaive, actuals
from ..forecast.scenarios import ConformalScenarios, crps_ensemble, interval_coverage
from .outcome import TRUE_COST, evaluate_plan, oracle


@dataclass
class Phase:
    """Fit on data up to ``fit_end``, calibrate scenarios on ``calib`` origins, evaluate on ``evaluate`` origins."""

    name: str
    fit_end: int
    calib: np.ndarray
    evaluate: np.ndarray


def weekly_origins(series: DailySeries, year: int, horizon: int, weekday: int = 6) -> np.ndarray:
    """Plan every Sunday for Monday..Sunday."""
    idx = series.year_indices(year)
    return np.array([i for i in idx if series.dates[i].dayofweek == weekday and i + horizon < len(series)])


def daily_origins(series: DailySeries, year: int, horizon: int) -> np.ndarray:
    idx = series.year_indices(year)
    return idx[(idx >= 27) & (idx + horizon < len(series))]


def standard_phases(series: DailySeries, horizon: int = 7) -> dict[str, Phase]:
    """Five years Y1..Y5: select on Y4 (fit Y1-Y2, calibrate Y3); test on Y5 (fit Y1-Y3, calibrate Y4)."""
    years = sorted(set(series.dates.year))
    if len(years) < 5:
        raise ValueError("standard_phases needs five calendar years of data")
    y = years[-5:]
    return {
        "select": Phase("select", series.year_indices(y[1])[-1], daily_origins(series, y[2], horizon),
                        weekly_origins(series, y[3], horizon)),
        "test": Phase("test", series.year_indices(y[2])[-1], daily_origins(series, y[3], horizon),
                      weekly_origins(series, y[4], horizon)),
    }


@dataclass
class PeriodForecasts:
    forecaster: str
    phase: str
    origins: np.ndarray
    dates: pd.DatetimeIndex
    point: np.ndarray  # (K, U, H)
    actual: np.ndarray  # (K, U, H)
    generator: ConformalScenarios

    def scenarios(self, k: int, scale: float = 1.0) -> np.ndarray:
        return self.generator.sample(self.point[k], key=int(self.origins[k]), scale=scale)

    def all_scenarios(self, scale: float = 1.0) -> np.ndarray:
        return np.stack([self.scenarios(k, scale) for k in range(len(self.origins))])


def forecast_period(
    series: DailySeries,
    forecaster: Forecaster,
    phase: Phase,
    horizon: int = 7,
    n_scenarios: int = 40,
    seed: int = 0,
    refit: bool = True,
) -> PeriodForecasts:
    if refit:
        forecaster.fit(series, phase.fit_end)
    calib_pred = forecaster.predict_many(series, phase.calib, horizon)
    gen = ConformalScenarios(n_scenarios, seed=seed).calibrate(actuals(series, phase.calib, horizon), calib_pred)
    return PeriodForecasts(
        forecaster=forecaster.name,
        phase=phase.name,
        origins=phase.evaluate,
        dates=series.dates[phase.evaluate],
        point=forecaster.predict_many(series, phase.evaluate, horizon),
        actual=actuals(series, phase.evaluate, horizon),
        generator=gen,
    )


def forecast_metrics(pf: PeriodForecasts, series: DailySeries, scale: float = 1.0) -> dict:
    err = pf.point - pf.actual
    naive = SeasonalNaive().predict_many(series, pf.origins, pf.point.shape[2])
    scen = pf.all_scenarios(scale)
    return {
        "forecaster": pf.forecaster,
        "phase": pf.phase,
        "scale": scale,
        "mae": float(np.abs(err).mean()),
        "rmse": float(np.sqrt((err**2).mean())),
        "mase_vs_naive": float(np.abs(err).mean() / np.abs(naive - pf.actual).mean()),
        "bias": float(err.mean()),
        "crps": crps_ensemble(scen, pf.actual),
        "coverage80": interval_coverage(scen, pf.actual, 0.8),
        "coverage90": interval_coverage(scen, pf.actual, 0.9),
    }


def _week_task(args):
    hospital, method, point, scenarios, actual, settings = args
    if method == "oracle":
        out = oracle(hospital, actual)
    else:
        out = evaluate_plan(hospital, make_plan(method, hospital, point, scenarios, settings), actual)
    return out.as_dict()


def plan_task(args):
    """Worker: (hospital, scenarios, settings) -> first-stage plan."""
    hospital, scenarios, settings = args
    return solve_allocation(hospital, scenarios, settings).first_stage()


def evaluate_task(args):
    """Worker: (hospital, first_stage, realised) -> outcome dict."""
    hospital, first_stage, realised = args
    return evaluate_plan(hospital, first_stage, realised).as_dict()


def _quiet_worker() -> None:
    # HiGHS occasionally prints diagnostics straight to stdout from C++; keep worker output clean.
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)


_POOL: dict = {}


def _executor(workers: int) -> ProcessPoolExecutor:
    """One worker pool per process, reused across calls (process start-up is the slow part)."""
    if workers not in _POOL:
        _POOL[workers] = ProcessPoolExecutor(max_workers=workers, initializer=_quiet_worker)
    return _POOL[workers]


def run_tasks(tasks: list, workers: int | None = None, fn=None) -> list:
    fn = fn or _week_task
    workers = workers or min(4, os.cpu_count() or 1)
    if workers <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]
    return list(_executor(workers).map(fn, tasks, chunksize=1))


def oracle_costs(hospital: HospitalConfig, pf: PeriodForecasts, workers: int | None = None) -> pd.DataFrame:
    """Perfect-foresight outcome for every evaluated week (independent of the forecaster)."""
    tasks = [(hospital, "oracle", None, None, pf.actual[k], TRUE_COST) for k in range(len(pf.origins))]
    df = pd.DataFrame(run_tasks(tasks, workers))
    df["week"] = np.arange(len(pf.origins))
    df["origin"] = pf.dates
    df["method"] = "oracle"
    return df


def backtest(
    hospital: HospitalConfig,
    pf: PeriodForecasts,
    methods,
    settings: PlanSettings = PlanSettings(),
    scale: float = 1.0,
    workers: int | None = None,
    label: str | None = None,
    oracle_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per (method, week) with realised outcome and regret against perfect foresight.

    Pass ``oracle_df`` (from ``oracle_costs``) to reuse perfect-foresight results across calls;
    the oracle rows are then not repeated in the output.
    """
    methods = list(methods)
    tasks, keys = [], []
    for m in methods:
        for k in range(len(pf.origins)):
            scen = pf.scenarios(k, scale) if m not in ("rule", "deterministic") else None
            tasks.append((hospital, m, pf.point[k], scen, pf.actual[k], settings))
            keys.append((m, k))
    df = pd.DataFrame(run_tasks(tasks, workers))
    df["method"] = [m for m, _ in keys]
    df["week"] = [k for _, k in keys]
    df["origin"] = [pf.dates[k] for _, k in keys]
    if oracle_df is None:
        oracle_df = oracle_costs(hospital, pf, workers)
        df = pd.concat([df, oracle_df], ignore_index=True)
    df["forecaster"] = pf.forecaster
    df["phase"] = pf.phase
    df["scale"] = scale
    df["label"] = label or pf.phase
    df["oracle_cost"] = df["week"].map(oracle_df.set_index("week")["total_cost"])
    df["regret"] = df["total_cost"] - df["oracle_cost"]
    return df


def summarise(df: pd.DataFrame, by=("label", "method")) -> pd.DataFrame:
    """Per-method annual totals and weekly averages."""
    def agg(g: pd.DataFrame) -> pd.Series:
        return pd.Series(
            {
                "weeks": len(g),
                "mean_weekly_cost": g.total_cost.mean(),
                "mean_weekly_regret": g.regret.mean(),
                "regret_pct": 100 * g.regret.sum() / g.oracle_cost.sum(),
                "cvar90_weekly_cost": cvar(g.total_cost.to_numpy(), 0.9),
                "shortage_patient_days": g.shortage_patient_days.sum(),
                "shortage_unit_day_rate_pct": 100 * g.shortage_unit_days.sum() / g.unit_days.sum(),
                "outlier_patient_days": g.outlier_patient_days.sum(),
                "idle_bed_days": g.idle_bed_days.sum(),
                "excess_nurse_days": g.excess_nurse_days.sum(),
                "agency_nurse_days": g.agency_nurse_days.sum(),
                "surge_bed_days": g.surge_bed_days.sum(),
                "nurse_moves": g.nurse_moves.sum(),
                "pump_moves": g.pump_moves.sum(),
            }
        )

    return df.groupby(list(by), sort=False).apply(agg, include_groups=False).reset_index()


def paired_bootstrap(a: np.ndarray, b: np.ndarray, n: int = 5000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of (a - b) per week and a 95% bootstrap interval."""
    d = np.asarray(a) - np.asarray(b)
    rng = np.random.default_rng(seed)
    boots = d[rng.integers(0, len(d), (n, len(d)))].mean(axis=1)
    return float(d.mean()), float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))
