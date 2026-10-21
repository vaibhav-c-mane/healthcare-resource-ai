"""Planning service used by the CLI and the offline app.

Everything runs locally: forecasts, scenarios, the optimiser, and a SQLite
log of recommendations and administrator decisions. Only aggregate counts
are stored; no patient-level data is read or written.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .allocate.model import Plan, solve_allocation
from .config import HospitalConfig, PlanSettings, default_hospital
from .data.series import DailySeries
from .forecast.models import FlowModel, Forecaster, GradientBoosting, HoltWinters, NegBinGLM, SeasonalNaive, actuals
from .forecast.scenarios import ConformalScenarios
from .overrides.learner import OverrideLearner

FORECASTERS = {
    "flow": FlowModel,
    "gbm": GradientBoosting,
    "negbin_glm": NegBinGLM,
    "holt_winters": HoltWinters,
    "seasonal_naive": SeasonalNaive,
}

REASON_CODES = (
    "known event not in data (e.g. elective list change)",
    "staff availability (sickness, leave)",
    "equipment out of service",
    "risk appetite: want more buffer",
    "risk appetite: want less buffer",
    "data looks wrong",
    "other",
)


@dataclass
class PlanResult:
    origin: pd.Timestamp
    days: pd.DatetimeIndex
    units: list[str]
    point: np.ndarray  # (U, H)
    scenarios: np.ndarray  # (S, U, H)
    plan: Plan
    forecaster: str
    settings: PlanSettings

    def band(self, level: float = 0.8) -> tuple[np.ndarray, np.ndarray]:
        lo = np.quantile(self.scenarios, (1 - level) / 2, axis=0)
        hi = np.quantile(self.scenarios, 1 - (1 - level) / 2, axis=0)
        return lo, hi

    def table(self) -> pd.DataFrame:
        lo, hi = self.band()
        rows = []
        for j, unit in enumerate(self.units):
            for t, day in enumerate(self.days):
                rows.append({
                    "day": day.date(), "unit": unit,
                    "forecast_median": round(float(np.median(self.scenarios[:, j, t])), 1),
                    "forecast_p10": round(float(lo[j, t]), 1), "forecast_p90": round(float(hi[j, t]), 1),
                    "beds": int(self.plan.beds[j, t]), "nurses": int(self.plan.nurses[j, t]),
                    "pumps": int(self.plan.pumps[j, t]),
                })
        return pd.DataFrame(rows)


def make_forecaster(name: str) -> Forecaster:
    return FORECASTERS[name]()


def plan_next_week(
    series: DailySeries,
    origin=None,
    forecaster: str = "flow",
    settings: PlanSettings = PlanSettings(),
    hospital: HospitalConfig | None = None,
    horizon: int = 7,
    n_scenarios: int = 40,
    scale: float = 1.0,
    calibration_days: int = 365,
) -> PlanResult:
    """Recommend beds, nurses and pumps for the ``horizon`` days after ``origin`` (default: last day of data).

    The forecaster is fitted on data before the calibration window, scenarios are
    calibrated on the window's forecast errors, then the forecaster is refitted on
    everything up to the origin.
    """
    hospital = hospital or default_hospital()
    o = len(series) - 1 if origin is None else series.index_of(origin)
    calib = np.arange(max(28, o - calibration_days - horizon + 1), o - horizon + 1)
    if len(calib) < 30:
        raise ValueError("need at least about 3 months of history before the origin")
    f = make_forecaster(forecaster).fit(series, int(calib[0]) - 1)
    gen = ConformalScenarios(n_scenarios, scale=scale).calibrate(
        actuals(series, calib, horizon), f.predict_many(series, calib, horizon)
    )
    f.fit(series, o)
    point = f.predict(series, o, horizon)
    scen = gen.sample(point, key=o)
    plan = solve_allocation(hospital, scen, settings)
    days = pd.date_range(series.dates[o] + pd.Timedelta(days=1), periods=horizon, freq="D")
    return PlanResult(series.dates[o], days, series.units, point, scen, plan, forecaster, settings)


class DecisionLog:
    """Local SQLite store of recommendations, final decisions and reasons (aggregates only)."""

    def __init__(self, path: str | Path = ".hcalloc/decisions.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_utc TEXT, origin TEXT, forecaster TEXT, settings TEXT,
                    units TEXT, recommended TEXT, final TEXT, scenarios BLOB, reason TEXT, note TEXT)"""
            )

    def _conn(self):
        return sqlite3.connect(self.path)

    def record(self, result: PlanResult, final: dict, reason: str = "", note: str = "") -> int:
        rec = {k: np.asarray(v).tolist() for k, v in result.plan.first_stage().items()}
        fin = {k: np.asarray(v).tolist() for k, v in final.items()}
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO decisions (created_utc, origin, forecaster, settings, units, recommended, final, scenarios, reason, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(), str(result.origin.date()), result.forecaster,
                    json.dumps({"risk_weight": result.settings.risk_weight,
                                "shortage_multiplier": result.settings.shortage_multiplier}),
                    json.dumps(result.units), json.dumps(rec), json.dumps(fin),
                    result.scenarios.astype(np.float32).tobytes(), reason, note,
                ),
            )
            return int(cur.lastrowid)

    def load(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, origin, units, recommended, final, scenarios, reason FROM decisions ORDER BY id"
            ).fetchall()
        out = []
        for rid, origin, units, rec, fin, blob, reason in rows:
            rec, fin = json.loads(rec), json.loads(fin)
            shape = (-1, len(json.loads(units)), len(rec["beds"][0]))
            out.append({
                "id": rid, "origin": origin, "reason": reason,
                "recommended": {k: np.array(v) for k, v in rec.items()},
                "final": {k: np.array(v) for k, v in fin.items()},
                "scenarios": np.frombuffer(blob, dtype=np.float32).reshape(shape).astype(float),
            })
        return out


def recalibrate(log: DecisionLog, hospital: HospitalConfig | None = None, base: PlanSettings = PlanSettings(),
                learner: OverrideLearner | None = None) -> tuple[tuple[float, float], OverrideLearner]:
    """Re-estimate cost weights from logged decisions. Returns the suggestion; applying it is a human step."""
    hospital = hospital or default_hospital()
    learner = learner or OverrideLearner(prior=(base.shortage_multiplier, base.risk_weight))
    for row in log.load():
        cands = {t: solve_allocation(hospital, row["scenarios"], learner.settings(t, base)).first_stage()
                 for t in learner.grid}
        learner.observe(cands, row["final"])
    return learner.estimate(), learner
