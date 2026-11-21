"""Planning service behind the bed dashboard: forecast, plan, and explain one day's recommendation.

``recommend`` returns plain JSON-ready data, so any front end (the bundled page,
or the team's React app through the FastAPI endpoint) can show it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..data.series import DailySeries
from ..forecast.models import HoltWinters, actuals, valid_origins
from ..forecast.scenarios import ConformalScenarios
from .model import NetworkConfig, plan_coordinated
from .simulate import NetworkSimSpec, simulate_network


def forecast_scenarios(series: DailySeries, origin: int, horizon: int = 7, n_scenarios: int = 40, calib_days: int = 180,
                       seed: int = 0):
    """Holt-Winters point forecast + conformal joint scenarios calibrated on the ``calib_days`` before the origin."""
    f = HoltWinters()
    fit_end = origin - calib_days - horizon
    if fit_end < 60:
        raise ValueError("need at least about 250 days of history before the planning date")
    f.fit(series, fit_end)
    cal = valid_origins(series, np.arange(origin - calib_days - horizon, origin - horizon + 1), horizon)
    gen = ConformalScenarios(n_scenarios, seed=seed).calibrate(actuals(series, cal, horizon), f.predict_many(series, cal, horizon))
    point = f.predict_many(series, [origin], horizon)[0]
    return point, gen.sample(point, key=origin)


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def recommend(series: DailySeries, cfg: NetworkConfig, origin: int, horizon: int = 7, n_scenarios: int = 40) -> dict:
    point, scen = forecast_scenarios(series, origin, horizon, n_scenarios)
    plan = plan_coordinated(cfg, scen)
    dates = [series.dates[origin] + pd.Timedelta(days=h + 1) for h in range(horizon)]
    beds = np.asarray(cfg.beds, dtype=float)
    p10, p50, p90 = (np.quantile(scen, q, axis=0) for q in (0.1, 0.5, 0.9))
    hospitals = []
    for n, name in enumerate(cfg.names):
        reserve = plan.open[n] - np.round(p50[n])
        peak = int(np.argmax(p90[n]))
        reasons = []
        if p90[n, peak] > beds[n]:
            reasons.append(f"On {dates[peak]:%a %d %b} there is a 1-in-10 chance demand reaches {p90[n, peak]:.0f}, "
                           f"above all {beds[n]:.0f} beds; the plan relies on transfers out.")
        elif p90[n, peak] > 0.95 * beds[n]:
            reasons.append(f"Busiest day {dates[peak]:%a %d %b}: high case {p90[n, peak]:.0f} beds "
                           f"({_pct(p90[n, peak] / beds[n])} of beds), so reserves are kept.")
        else:
            reasons.append(f"Forecast stays below {_pct(p90[n].max() / beds[n])} of beds in the high case; "
                           f"spare beds can take transfers.")
        if reserve.mean() > 0:
            reasons.append(f"Opens on average {reserve.mean():.0f} beds above the median forecast as a reserve, "
                           f"because an unplaced patient is assumed to cost {cfg.unmet_cost / cfg.open_cost:.0f}x an open bed.")
        hospitals.append({
            "name": name, "beds": int(beds[n]), "transfer_cap_per_day": int(cfg.transfer_cap[n]),
            "occupied_today": None if math.isnan(series.census[origin, n]) else int(series.census[origin, n]),
            "days": [{"date": f"{d:%Y-%m-%d}", "p10": round(float(p10[n, h]), 1), "p50": round(float(p50[n, h]), 1),
                      "p90": round(float(p90[n, h]), 1), "open_beds": int(plan.open[n, h]),
                      "reserve": int(reserve[h]), "expected_unplaced": round(float(plan.expected_unmet[n, h]), 2)}
                     for h, d in enumerate(dates)],
            "reasons": reasons,
        })
    transfers = []
    mv = plan.expected_moves
    for h, d in enumerate(dates):
        for i in range(cfg.n):
            for j in range(cfg.n):
                if mv[i, j, h] >= 0.5:
                    transfers.append({
                        "date": f"{d:%Y-%m-%d}", "from": cfg.names[i], "to": cfg.names[j],
                        "expected_patients": round(float(mv[i, j, h]), 1),
                        "reason": (f"In its busiest scenarios {cfg.names[i]} needs up to {np.quantile(scen[:, i, h], 0.95):.0f} "
                                   f"of its {beds[i]:.0f} beds, while "
                                   f"{cfg.names[j]} is expected at {_pct(p50[j, h] / beds[j])}; up to "
                                   f"{cfg.transfer_cap[i]} patients a day may move."),
                    })
    return {
        "planned_on": f"{series.dates[origin]:%Y-%m-%d}",
        "horizon_days": horizon,
        "data": series.provenance.get("kind", "unknown"),
        "costs": {"open_bed_day": cfg.open_cost, "transfer": cfg.transfer_cost, "unplaced_patient_day": cfg.unmet_cost},
        "hospitals": hospitals,
        "transfers": transfers,
        "notes": ["Recommendations are advisory. A bed manager decides.",
                  "Transfers are planned on average over 40 demand scenarios; the number actually needed depends on the day."],
    }


class DemoSource:
    """Three SYNTHETIC hospitals from the network simulator, simultaneous-surge regime, test year."""

    def __init__(self, regime: str = "simultaneous", seed: int = 0, cap_share: float = 0.02,
                 spec: NetworkSimSpec = NetworkSimSpec()):
        self.series, self.surge = simulate_network(regime, seed, spec)
        self.cfg = NetworkConfig.from_beds(spec.names, spec.beds, cap_share=cap_share)
        test = np.flatnonzero(self.series.dates.year == self.series.dates.year.max())
        surge_days = test[self.surge[test].any(axis=1)]
        self.default_origin = int(surge_days[0] + 3) if len(surge_days) else int(test[60])
        self.valid = test[: -8]

    def origin_for(self, date: str | None) -> int:
        if not date:
            return self.default_origin
        i = self.series.index_of(date)
        if i not in set(self.valid.tolist()):
            raise ValueError(f"pick a date in the test year between {self.series.dates[self.valid[0]]:%Y-%m-%d} "
                             f"and {self.series.dates[self.valid[-1]]:%Y-%m-%d}")
        return i
