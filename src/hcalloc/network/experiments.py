"""Network experiments.

E1  main comparison of policies, simulated network (baseline regime) and real NHS trust networks
E2  surges: local, simultaneous and near-capacity regimes on the simulated network; peak weeks on NHS data
E3  sensitivity of the coordination gain to transfer cost and the daily transfer cap

Main measures (sums over the test period): unmet bed-days, idle open bed-days, transfers.
Secondary: cost and regret against perfect foresight under the same beds, cap and costs.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from ..data.series import DailySeries
from ..evaluate.backtest import Phase, forecast_period, paired_bootstrap, season_phases, standard_phases
from ..forecast.models import GradientBoosting, HoltWinters
from .model import NetworkConfig, evaluate_plan, oracle
from .policies import METHOD, POLICIES, make_policy_plan
from .simulate import REGIMES, NetworkSimSpec, simulate_network

MEASURES = ("unmet_bed_days", "idle_bed_days", "transfers", "cost", "regret")


@dataclass
class NetworkRunConfig:
    horizon: int = 7
    n_scenarios: int = 40
    seeds: tuple[int, ...] = (0, 1, 2, 3, 4)
    regimes: tuple[str, ...] = REGIMES
    cap_share: float = 0.02
    open_cost: float = 1.0
    transfer_cost: float = 1.5
    unmet_cost: float = 8.0
    sweep_transfer_cost: tuple[float, ...] = (0.5, 1.5, 3.0, 6.0)
    sweep_cap_share: tuple[float, ...] = (0.0, 0.01, 0.02, 0.05)
    workers: int = 4
    max_weeks: int | None = None  # for smoke tests
    spec: NetworkSimSpec = field(default_factory=NetworkSimSpec)

    def network(self, names, beds, **over) -> NetworkConfig:
        kw = dict(cap_share=self.cap_share, open_cost=self.open_cost, transfer_cost=self.transfer_cost,
                  unmet_cost=self.unmet_cost)
        kw.update(over)
        return NetworkConfig.from_beds(names, beds, **kw)


def choose_forecaster(series: DailySeries, phases: dict[str, Phase], horizon: int, n_scenarios: int):
    """Pick the census-only forecaster with the lowest MAE on the selection period."""
    best = None
    for f in (HoltWinters(), GradientBoosting(relative=True)):
        pf = forecast_period(series, f, phases["select"], horizon, n_scenarios)
        mae = float(np.abs(pf.point - pf.actual).mean())
        if best is None or mae < best[0]:
            best = (mae, f)
    return best[1], best[0]


def score_period(cfg: NetworkConfig, pf, policies=POLICIES, flags: np.ndarray | None = None, label: str = "",
                 seed: int = 0) -> pd.DataFrame:
    """One row per (week, policy). ``flags``: (K,) bool marking peak or surge weeks."""
    rows = []
    for k in range(len(pf.origins)):
        scen = pf.scenarios(k)
        point = pf.point[k]
        actual = pf.actual[k]
        orc = oracle(cfg, actual)
        for pol in policies:
            plan, eval_cfg = make_policy_plan(pol, cfg, scen, point, seed=int(pf.origins[k]))
            out = evaluate_plan(eval_cfg, plan.open, actual)
            rows.append({
                "label": label, "seed": seed, "origin": str(pf.dates[k].date()), "week": k, "policy": pol,
                "flag": bool(flags[k]) if flags is not None else False,
                "unmet_bed_days": out.unmet_bed_days, "idle_bed_days": out.idle_bed_days,
                "transfers": out.transfers, "cost": out.cost, "oracle_cost": orc.cost,
                "regret": out.cost - orc.cost, "open_bed_days": float(np.sum(plan.open)),
                "oracle_unmet": orc.unmet_bed_days, "oracle_transfers": orc.transfers,
            })
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame, by=("label", "policy")) -> pd.DataFrame:
    """Totals per policy plus the paired difference to the method (coordinated) with a 95% bootstrap interval."""
    out = []
    for keys, g in df.groupby(list(by), sort=False):
        keys = keys if isinstance(keys, tuple) else (keys,)
        row = dict(zip(by, keys))
        row.update({
            "weeks": len(g),
            "unmet_bed_days": g.unmet_bed_days.sum(),
            "idle_bed_days": g.idle_bed_days.sum(),
            "transfers": g.transfers.sum(),
            "cost": g.cost.sum(),
            "regret_pct": 100 * g.regret.sum() / g.oracle_cost.sum(),
        })
        base_keys = {k: v for k, v in row.items() if k in by and k != "policy"}
        ref = df
        for k, v in base_keys.items():
            ref = ref[ref[k] == v]
        ref = ref[ref.policy == METHOD]
        merged = g.merge(ref, on=[c for c in ("label", "seed", "week") if c in g], suffixes=("", "_m"))
        for m in ("unmet_bed_days", "idle_bed_days", "cost"):
            d, lo, hi = paired_bootstrap(merged[m].to_numpy(), merged[f"{m}_m"].to_numpy())
            row[f"{m}_vs_method_per_week"] = d
            row[f"{m}_vs_method_lo"] = lo
            row[f"{m}_vs_method_hi"] = hi
        out.append(row)
    return pd.DataFrame(out)


# ---------- simulated network ----------

def _sim_task(args):
    regime, seed, rc = args
    series, surge = simulate_network(regime, seed, rc.spec)
    phases = standard_phases(series, rc.horizon)
    if rc.max_weeks:
        t = phases["test"]
        phases["test"] = Phase(t.name, t.fit_end, t.calib, t.evaluate[: rc.max_weeks])
    f, mae = choose_forecaster(series, phases, rc.horizon, rc.n_scenarios)
    pf = forecast_period(series, f, phases["test"], rc.horizon, rc.n_scenarios, seed=seed)
    idx = pf.origins[:, None] + np.arange(1, rc.horizon + 1)[None]
    flags = surge[idx].any(axis=(1, 2))
    cfg = rc.network(rc.spec.names, rc.spec.beds)
    df = score_period(cfg, pf, flags=flags, label=regime, seed=seed)
    df["forecaster"] = f.name
    sweep = []
    if regime in ("simultaneous", "local"):
        for tc in rc.sweep_transfer_cost:
            for cs in rc.sweep_cap_share:
                c2 = rc.network(rc.spec.names, rc.spec.beds, cap_share=cs, transfer_cost=tc)
                d2 = score_period(c2, pf, policies=("coordinated", "alone_reactive", "isolated"), flags=flags,
                                  label=regime, seed=seed)
                d2["transfer_cost"], d2["cap_share"] = tc, cs
                sweep.append(d2)
    meta = {"regime": regime, "seed": seed, "forecaster": f.name, "select_mae": mae,
            "test_mae": float(np.abs(pf.point - pf.actual).mean())}
    return df, (pd.concat(sweep) if sweep else None), meta


def _single_thread_worker() -> None:
    """Each worker process uses one thread. Several processes each starting one thread per core
    (scikit-learn's OpenMP) oversubscribe the CPU and can slow gradient boosting down a thousandfold."""
    from threadpoolctl import threadpool_limits

    threadpool_limits(1)


def run_simulated(rc: NetworkRunConfig, log=print) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    tasks = [(r, s, rc) for r in rc.regimes for s in rc.seeds]
    t0 = time.time()
    if rc.workers > 1:
        with ProcessPoolExecutor(rc.workers, initializer=_single_thread_worker) as ex:
            res = list(ex.map(_sim_task, tasks))
    else:
        res = [_sim_task(t) for t in tasks]
    log(f"simulated network: {len(tasks)} runs in {time.time() - t0:.0f}s")
    weekly = pd.concat([r[0] for r in res], ignore_index=True)
    sweeps = [r[1] for r in res if r[1] is not None]
    sweep = pd.concat(sweeps, ignore_index=True) if sweeps else pd.DataFrame()
    return weekly, sweep, [r[2] for r in res]


# ---------- real NHS trusts ----------

NHS_NETWORKS = {
    # neighbouring acute trusts, so transfers between them are plausible
    "tyne_and_wear": ("RTD", "RR7", "R0B"),  # Newcastle, Gateshead, South Tyneside and Sunderland
    "greater_manchester": ("RM3", "RMC", "RRF"),  # Northern Care Alliance, Bolton, Wrightington Wigan and Leigh
}


def nhs_network_series(ga: pd.DataFrame, trusts: tuple[str, ...], max_fill_days: int = 3) -> tuple[DailySeries, dict]:
    """Adult G&A beds occupied per trust as one census-only series (units = trusts)."""
    df = ga[ga.trust_code.isin(trusts) & (ga.unit == "G&A")]
    wide = df.pivot_table(index="date", columns="trust_code", values="beds_occupied", aggfunc="first")
    wide = wide.reindex(columns=list(trusts))
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
    published = full.isin(pd.DatetimeIndex(df.date.unique()))
    wide = wide.reindex(full).interpolate(limit=max_fill_days, limit_area="inside")
    wide[~published] = np.nan
    census = wide.to_numpy(dtype=float, copy=True)
    observed = ~np.isnan(census).any(axis=1)
    census[~observed] = np.nan
    nan = np.full_like(census, np.nan)
    names = df.drop_duplicates("trust_code", keep="last").set_index("trust_code").trust_name.to_dict()
    series = DailySeries(full, list(trusts), census, nan, nan.copy(), observed=observed,
                         provenance={"kind": "real (NHS England UEC daily sitrep, adult G&A beds occupied)",
                                     "trusts": {t: names.get(t, t) for t in trusts},
                                     "licence": "Open Government Licence v3.0"})
    return series, names


def run_nhs(ga: pd.DataFrame, rc: NetworkRunConfig, networks=NHS_NETWORKS, log=print):
    weekly, sweeps, metas = [], [], []
    for name, trusts in networks.items():
        series, names = nhs_network_series(ga, trusts)
        phases = season_phases(series, rc.horizon)
        test_start = series.dates[phases["test"].evaluate[0]]
        before = ga[ga.trust_code.isin(trusts) & (ga.unit == "G&A") & (ga.date < test_start)]
        beds = tuple(int(before[before.trust_code == t].beds_open.max()) for t in trusts)
        if rc.max_weeks:
            t = phases["test"]
            phases["test"] = Phase(t.name, t.fit_end, t.calib, t.evaluate[: rc.max_weeks])
        f, mae = choose_forecaster(series, phases, rc.horizon, rc.n_scenarios)
        pf = forecast_period(series, f, phases["test"], rc.horizon, rc.n_scenarios)
        load = pf.actual.sum(axis=1).mean(axis=1)  # network occupied beds per week
        flags = load >= np.quantile(load, 0.75)  # peak weeks = busiest quarter of the winter
        cfg = rc.network(trusts, beds)
        df = score_period(cfg, pf, flags=flags, label=name)
        df["forecaster"] = f.name
        weekly.append(df)
        for tc in rc.sweep_transfer_cost:
            for cs in rc.sweep_cap_share:
                c2 = rc.network(trusts, beds, cap_share=cs, transfer_cost=tc)
                d2 = score_period(c2, pf, policies=("coordinated", "alone_reactive", "isolated"), flags=flags, label=name)
                d2["transfer_cost"], d2["cap_share"] = tc, cs
                sweeps.append(d2)
        occ = (pf.actual / np.asarray(beds)[None, :, None]).mean(axis=(0, 2))
        metas.append({"network": name, "trusts": {t: names.get(t, t) for t in trusts}, "beds": dict(zip(trusts, beds)),
                      "transfer_cap_per_day": dict(zip(trusts, cfg.transfer_cap)),
                      "mean_test_occupancy": dict(zip(trusts, occ.round(3).tolist())),
                      "test_weeks": len(pf.origins), "test_from": str(pf.dates[0].date()), "test_to": str(pf.dates[-1].date()),
                      "forecaster": f.name, "select_mae": mae, "test_mae": float(np.abs(pf.point - pf.actual).mean())})
        log(f"NHS {name}: {len(pf.origins)} weeks, forecaster {f.name}")
    return pd.concat(weekly, ignore_index=True), pd.concat(sweeps, ignore_index=True), metas


def run_all(out: str | Path = "results/network", rc: NetworkRunConfig = NetworkRunConfig(), ga_csv: str | None = None,
            log=print) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    weekly, sweep, metas = run_simulated(rc, log)
    weekly.to_csv(out / "sim_weekly.csv", index=False)
    if len(sweep):
        sweep.to_csv(out / "sim_sweep_weekly.csv", index=False)
    e1e2 = summarise(weekly, by=("label", "policy"))
    e1e2.to_csv(out / "sim_summary.csv", index=False)
    surge = summarise(weekly[weekly.flag], by=("label", "policy"))
    surge.to_csv(out / "sim_surge_weeks_summary.csv", index=False)
    if len(sweep):
        summarise(sweep, by=("label", "transfer_cost", "cap_share", "policy")).to_csv(out / "sim_sweep_summary.csv", index=False)
    result = {"simulated": metas, "config": {k: v for k, v in asdict(rc).items() if k != "spec"}, "spec": asdict(rc.spec)}
    if ga_csv:
        ga = pd.read_csv(ga_csv, parse_dates=["date"])
        nw, ns, nm = run_nhs(ga, rc, log=log)
        nw.to_csv(out / "nhs_weekly.csv", index=False)
        ns.to_csv(out / "nhs_sweep_weekly.csv", index=False)
        summarise(nw, by=("label", "policy")).to_csv(out / "nhs_summary.csv", index=False)
        summarise(nw[nw.flag], by=("label", "policy")).to_csv(out / "nhs_peak_weeks_summary.csv", index=False)
        summarise(ns, by=("label", "transfer_cost", "cap_share", "policy")).to_csv(out / "nhs_sweep_summary.csv", index=False)
        result["nhs"] = nm
    (out / "run_metadata.json").write_text(json.dumps(result, indent=2, default=str))
    return result
