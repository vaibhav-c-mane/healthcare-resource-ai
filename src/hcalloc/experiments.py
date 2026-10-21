"""The benchmark experiments from the project plan, run end to end.

1. Method comparison: does uncertainty-aware, coupled planning beat the baselines?
2. Decision-based selection: does choosing the forecaster by decision regret beat choosing by MAE?
   (The method comparison is also repeated on independently simulated hospitals, one per seed.)
3. Override learning: can the system recover an administrator's hidden cost weights?
4. Sensitivity: do the conclusions survive changes to the main cost and capacity assumptions?

All results are on SEMI-SYNTHETIC data (see hcalloc.data.synthetic) and are
written to ``out_dir`` as CSV files, figures and a REPORT.md with provenance.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from .allocate.baselines import METHODS
from .config import HospitalConfig, PlanSettings, default_hospital
from .data.series import DailySeries
from .data.synthetic import StressWave, simulate_hospital
from .evaluate.backtest import (
    PeriodForecasts,
    Phase,
    backtest,
    evaluate_task,
    forecast_metrics,
    forecast_period,
    oracle_costs,
    paired_bootstrap,
    plan_task,
    run_tasks,
    standard_phases,
    summarise,
)
from .forecast.models import actuals, default_forecasters
from .overrides.admin_sim import SimulatedAdministrator
from .overrides.learner import OverrideLearner, override_share, plan_distance


@dataclass
class RunConfig:
    seed: int = 0
    horizon: int = 7
    n_scenarios: int = 40
    scales: tuple[float, ...] = (0.75, 1.0, 1.25, 1.5)
    stress_start: str = "2023-10-01"
    workers: int = 4
    max_weeks: int | None = None  # limit evaluated weeks (for quick runs)
    replication_seeds: tuple[int, ...] = (1, 2, 3, 4)  # extra simulated hospitals for the method comparison
    settings: PlanSettings = field(default_factory=PlanSettings)


def _limit(pf: PeriodForecasts, n: int | None) -> PeriodForecasts:
    if n is None:
        return pf
    return PeriodForecasts(pf.forecaster, pf.phase, pf.origins[:n], pf.dates[:n], pf.point[:n], pf.actual[:n], pf.generator)


def build_forecasts(normal: DailySeries, stress: DailySeries, phases: dict[str, Phase], cfg: RunConfig) -> dict:
    pfs = {}
    stress_phase = Phase("stress", phases["test"].fit_end, phases["test"].calib, phases["test"].evaluate)
    for f in default_forecasters():
        common = dict(horizon=cfg.horizon, n_scenarios=cfg.n_scenarios, seed=cfg.seed)
        pfs[f.name, "select"] = _limit(forecast_period(normal, f, phases["select"], **common), cfg.max_weeks)
        pfs[f.name, "test"] = _limit(forecast_period(normal, f, phases["test"], **common), cfg.max_weeks)
        # stress data equals normal data before the wave starts, so the test-phase fit is reused
        pfs[f.name, "stress"] = _limit(forecast_period(stress, f, stress_phase, refit=False, **common), cfg.max_weeks)
    return pfs


def selection_experiment(hospital, pfs, oracles, cfg: RunConfig, series_by_phase) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score every (forecaster, scale) by forecast metrics and by decision regret, on select and test phases."""
    rows, weekly = [], []
    names = sorted({k[0] for k in pfs})
    for phase in ("select", "test"):
        for name in names:
            for scale in cfg.scales:
                pf = pfs[name, phase]
                df = backtest(hospital, pf, ["joint_cvar"], cfg.settings, scale=scale, workers=cfg.workers,
                              label=f"{name}|{scale}", oracle_df=oracles[phase])
                weekly.append(df)
                fm = forecast_metrics(pf, series_by_phase[phase], scale)
                fm.update(mean_weekly_regret=df.regret.mean(), regret_pct=100 * df.regret.sum() / df.oracle_cost.sum(),
                          shortage_patient_days=df.shortage_patient_days.sum())
                rows.append(fm)
    return pd.DataFrame(rows), pd.concat(weekly, ignore_index=True)


def choose_configs(sel: pd.DataFrame) -> dict:
    s = sel[sel.phase == "select"]
    by_mae = s[s.scale == 1.0].sort_values("mae").iloc[0]
    by_crps = s[s.scale == 1.0].sort_values("crps").iloc[0]
    by_regret = s.sort_values("mean_weekly_regret").iloc[0]
    base = s[s.scale == 1.0].set_index("forecaster")
    rho_mae = spearmanr(base["mae"], base["mean_weekly_regret"]).statistic
    rho_crps = spearmanr(base["crps"], base["mean_weekly_regret"]).statistic
    return {
        "mae": (by_mae.forecaster, 1.0),
        "crps": (by_crps.forecaster, 1.0),
        "regret": (by_regret.forecaster, float(by_regret.scale)),
        "spearman_mae_vs_regret": float(rho_mae),
        "spearman_crps_vs_regret": float(rho_crps),
    }


def comparison_experiment(hospital, pfs, oracles, choice, cfg: RunConfig) -> pd.DataFrame:
    """All methods on the test year (normal and stress), forecasts from the MAE-selected model."""
    frames = []
    f_mae = choice["mae"][0]
    f_reg, s_reg = choice["regret"]
    for label in ("test", "stress"):
        df = backtest(hospital, pfs[f_mae, label], METHODS, cfg.settings, workers=cfg.workers, label=label)
        dfs = backtest(hospital, pfs[f_reg, label], ["joint_cvar"], cfg.settings, scale=s_reg, workers=cfg.workers,
                       label=label, oracle_df=oracles[label])
        dfs["method"] = "joint_cvar_decision_selected"
        frames += [df, dfs]
    return pd.concat(frames, ignore_index=True)


def override_experiment(hospital: HospitalConfig, pf: PeriodForecasts, scale: float, cfg: RunConfig,
                        admin: SimulatedAdministrator | None = None, learner: OverrideLearner | None = None) -> pd.DataFrame:
    admin = admin or SimulatedAdministrator(seed=cfg.seed)
    learner = learner or OverrideLearner()
    k_weeks = len(pf.origins)
    grid = learner.grid
    scen = [pf.scenarios(k, scale) for k in range(k_weeks)]
    tasks = [(hospital, scen[k], learner.settings(t, cfg.settings)) for k in range(k_weeks) for t in grid]
    admin_settings = PlanSettings(risk_weight=admin.risk_weight, shortage_multiplier=admin.shortage_multiplier)
    tasks += [(hospital, scen[k], admin_settings) for k in range(k_weeks)]
    plans = run_tasks(tasks, cfg.workers, fn=plan_task)
    cand = [{t: plans[k * len(grid) + i] for i, t in enumerate(grid)} for k in range(k_weeks)]
    preferred = plans[k_weeks * len(grid):]

    rows, finals, recs, statics = [], [], [], []
    for k in range(k_weeks):
        theta = learner.estimate()
        rec = cand[k][theta]
        static = cand[k][learner.prior]
        final = admin.finalise(hospital, preferred[k], key=int(pf.origins[k]))
        rows.append({
            "week": k,
            "origin": pf.dates[k],
            "learned_multiplier": theta[0],
            "learned_risk_weight": theta[1],
            "override_l1_learned": plan_distance(rec, final),
            "override_l1_static": plan_distance(static, final),
            "override_share_learned": override_share(rec, final),
            "override_share_static": override_share(static, final),
            "noise_floor_l1": plan_distance(preferred[k], final),
        })
        learner.observe(cand[k], final)
        finals.append(final); recs.append(rec); statics.append(static)

    ev = [(hospital, p, pf.actual[k]) for plans_ in (recs, statics, finals) for k, p in enumerate(plans_)]
    outs = run_tasks(ev, cfg.workers, fn=evaluate_task)
    df = pd.DataFrame(rows)
    for j, name in enumerate(("learned", "static", "admin_final")):
        o = outs[j * k_weeks:(j + 1) * k_weeks]
        df[f"cost_{name}"] = [x["total_cost"] for x in o]
        df[f"shortage_{name}"] = [x["shortage_patient_days"] for x in o]
    df.attrs["true_theta"] = (admin.shortage_multiplier, admin.risk_weight)
    return df


def sensitivity_variants(hospital: HospitalConfig) -> dict[str, HospitalConfig]:
    """One-at-a-time changes to the assumptions the conclusions are most likely to depend on."""
    from dataclasses import replace

    def units(**kw):
        return replace(hospital, units=tuple(replace(u, **{k: v(u) for k, v in kw.items()}) for u in hospital.units))

    return {
        "base": hospital,
        "shortage cost x0.5": hospital.with_shortage_multiplier(0.5),
        "shortage cost x2": hospital.with_shortage_multiplier(2.0),
        "pump fleet 130": replace(hospital, pump_fleet=130),
        "pump fleet 180": replace(hospital, pump_fleet=180),
        "bed cost 1.0 (beds as costly as a nurse)": units(bed_cost=lambda u: 1.0),
        "no patient overflow": replace(hospital, overflow=()),
    }


def sensitivity_experiment(pf: PeriodForecasts, cfg: RunConfig, methods=("deterministic", "sequential", "joint_cvar")) -> pd.DataFrame:
    frames = []
    for name, hosp in sensitivity_variants(default_hospital()).items():
        df = backtest(hosp, pf, methods, cfg.settings, workers=cfg.workers, label=name)
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def replication_experiment(cfg: RunConfig, methods=METHODS) -> pd.DataFrame:
    """Repeat the method comparison on independently simulated hospitals (one per seed).

    For each seed the forecaster is chosen by validation MAE, as in the main comparison.
    """
    frames = []
    hospital = default_hospital()
    for seed in cfg.replication_seeds:
        series = simulate_hospital(seed=seed)
        phases = standard_phases(series, cfg.horizon)
        sel = phases["select"]
        maes = {}
        for f in default_forecasters():
            f.fit(series, sel.fit_end)
            maes[f.name] = float(np.abs(f.predict_many(series, sel.evaluate, cfg.horizon)
                                        - actuals(series, sel.evaluate, cfg.horizon)).mean())
        name = min(maes, key=maes.get)
        f = next(x for x in default_forecasters() if x.name == name)
        pf = _limit(forecast_period(series, f, phases["test"], cfg.horizon, cfg.n_scenarios, seed), cfg.max_weeks)
        df = backtest(hospital, pf, methods, cfg.settings, workers=cfg.workers, label=f"seed {seed}")
        df["seed"] = seed
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def run_sensitivity(out_dir: str | Path = "results", cfg: RunConfig | None = None, forecaster: str = "flow") -> pd.DataFrame:
    cfg = cfg or RunConfig()
    normal = simulate_hospital(seed=cfg.seed)
    phases = standard_phases(normal, cfg.horizon)
    f = next(x for x in default_forecasters() if x.name == forecaster)
    pf = _limit(forecast_period(normal, f, phases["test"], cfg.horizon, cfg.n_scenarios, cfg.seed), cfg.max_weeks)
    df = sensitivity_experiment(pf, cfg)
    df.to_csv(Path(out_dir) / "sensitivity_weekly.csv", index=False)
    summary = summarise(df)
    summary.to_csv(Path(out_dir) / "sensitivity_summary.csv", index=False)
    return summary


def run_all(out_dir: str | Path = "results", cfg: RunConfig | None = None, log=print) -> dict:
    cfg = cfg or RunConfig()
    out = Path(out_dir)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    hospital = default_hospital()
    normal = simulate_hospital(seed=cfg.seed)
    stress = simulate_hospital(seed=cfg.seed, stress=StressWave(cfg.stress_start))
    phases = standard_phases(normal, cfg.horizon)

    log("forecasting ...")
    pfs = build_forecasts(normal, stress, phases, cfg)
    series_by_phase = {"select": normal, "test": normal, "stress": stress}
    fmetrics = pd.DataFrame([forecast_metrics(pf, series_by_phase[ph]) for (_, ph), pf in pfs.items()])
    fmetrics.to_csv(out / "forecast_metrics.csv", index=False)

    any_name = next(iter(pfs))[0]
    oracles = {ph: oracle_costs(hospital, pfs[any_name, ph], cfg.workers) for ph in ("select", "test", "stress")}

    log(f"selection experiment ... ({time.time() - t0:.0f}s)")
    sel, sel_weekly = selection_experiment(hospital, pfs, oracles, cfg, series_by_phase)
    sel.to_csv(out / "selection.csv", index=False)
    sel_weekly.to_csv(out / "selection_weekly.csv", index=False)
    choice = choose_configs(sel)

    log(f"method comparison ... ({time.time() - t0:.0f}s)")
    comp = comparison_experiment(hospital, pfs, oracles, choice, cfg)
    comp.to_csv(out / "comparison_weekly.csv", index=False)
    comp_summary = summarise(comp)
    comp_summary.to_csv(out / "comparison_summary.csv", index=False)

    log(f"override experiment ... ({time.time() - t0:.0f}s)")
    f_reg, s_reg = choice["regret"]
    ovr = override_experiment(hospital, pfs[f_reg, "test"], s_reg, cfg)
    ovr.to_csv(out / "overrides.csv", index=False)

    log(f"sensitivity ... ({time.time() - t0:.0f}s)")
    sens_weekly = sensitivity_experiment(pfs[choice["mae"][0], "test"], cfg)
    sens_weekly.to_csv(out / "sensitivity_weekly.csv", index=False)
    sens = summarise(sens_weekly)
    sens.to_csv(out / "sensitivity_summary.csv", index=False)

    log(f"replication on seeds {cfg.replication_seeds} ... ({time.time() - t0:.0f}s)")
    rep_weekly = replication_experiment(cfg)
    base = comp[(comp.label == "test") & comp.method.isin(list(METHODS) + ["oracle"])].assign(label=f"seed {cfg.seed}", seed=cfg.seed)
    rep_weekly = pd.concat([base, rep_weekly], ignore_index=True)
    rep_weekly.to_csv(out / "replication_weekly.csv", index=False)
    rep = summarise(rep_weekly)
    rep.to_csv(out / "replication_summary.csv", index=False)

    meta = {
        "data": {"kind": "semi-synthetic", "normal": normal.provenance, "stress": stress.provenance},
        "hospital": asdict(hospital),
        "run_config": {**asdict(cfg), "settings": asdict(cfg.settings)},
        "phases": {k: {"fit_end": str(normal.dates[v.fit_end].date()),
                       "calibration": [str(normal.dates[v.calib[0]].date()), str(normal.dates[v.calib[-1]].date())],
                       "evaluation_origins": len(v.evaluate)} for k, v in phases.items()},
        "choice": choice,
        "true_admin_theta": ovr.attrs["true_theta"],
        "runtime_seconds": round(time.time() - t0, 1),
    }
    (out / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    from .report import write_report

    meta["runtime_seconds"] = round(time.time() - t0, 1)
    (out / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    write_report(out, fmetrics, sel, choice, comp, comp_summary, ovr, meta, sel_weekly=sel_weekly, sens=sens, rep=rep)
    log(f"done in {time.time() - t0:.0f}s -> {out}")
    return meta
