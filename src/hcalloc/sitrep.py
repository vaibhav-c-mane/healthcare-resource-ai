"""Forecast-then-allocate on real NHS England bed occupancy (UEC daily sitreps).

Demand is real: adult general and acute (G&A) and adult critical care beds
occupied per trust per day, for each winter the sitreps cover. Everything
else (bed base, staffing ratios, nurse pools, pumps, costs) is assumed and
derived from the trust's published bed numbers by ``trust_hospital``.

Splits are by season (``season_phases``): select on the third winter, test on
the fourth. The sitreps publish census only, so the flow forecaster is not used.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .allocate.baselines import METHODS
from .config import HospitalConfig, PlanSettings, Unit
from .data.loaders import clean_sitreps, load_uec_sitreps, sitrep_series
from .data.series import DailySeries
from .evaluate.backtest import backtest, forecast_metrics, forecast_period, oracle_costs, paired_bootstrap, season_phases, summarise
from .experiments import choose_configs, selection_experiment

STATS_URL = "https://www.england.nhs.uk/statistics/wp-content/uploads/sites/2"
# "Web File Timeseries" workbooks, one per winter, as published by NHS England
SITREP_FILES: dict[str, str] = {
    "2021-22": f"{STATS_URL}/2022/04/UEC-Daily-SitRep-Web-File-Timeseries.xlsx",
    "2022-23": f"{STATS_URL}/2023/04/UEC-Daily-SitRep-Web-File-Timeseries.xlsx",
    "2023-24": f"{STATS_URL}/2024/04/Web-File-Timeseries-UEC-Daily-SitRep.xlsx",
    "2024-25": f"{STATS_URL}/2025/04/Web-File-Timeseries-UEC-Daily-SitRep.xlsx",
}

# A small general hospital, a mid-sized trust and a large teaching trust, all with every day published
DEFAULT_TRUSTS = ("RA9", "RNA", "RTD")
UNITS = ("ICU", "G&A")


def download_sitreps(out_dir: str | Path = "data/sitrep", files: dict[str, str] = SITREP_FILES) -> list[Path]:
    import urllib.request

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for season, url in files.items():
        path = out / f"uec_sitrep_{season}.xlsx"
        if not path.exists():
            urllib.request.urlretrieve(url, path)
        paths.append(path)
    return paths


@dataclass(frozen=True)
class StaffingAssumptions:
    """Assumed values that turn a trust's bed numbers into a ``HospitalConfig``. None of these are in the sitreps."""

    patients_per_nurse: dict = field(default_factory=lambda: {"ICU": 1.5, "G&A": 6.0})
    pumps_per_patient: dict = field(default_factory=lambda: {"ICU": 3.0, "G&A": 0.5})
    shortage_cost: dict = field(default_factory=lambda: {"ICU": 12.0, "G&A": 6.0})
    surge_share: dict = field(default_factory=lambda: {"ICU": 0.10, "G&A": 0.05})
    agency_share: dict = field(default_factory=lambda: {"ICU": 0.15, "G&A": 0.05})
    nurse_pool_factor: float = 1.10  # rostered nurses available / nurses needed with every bed full
    float_share: float = 0.08  # float-pool nurses as a share of the pool (wards only)
    pump_fleet_factor: float = 1.05  # pumps owned / pumps needed with every bed full (G&A runs near 97% full)
    pump_move_share: float = 0.10  # pump transfers per day as a share of the fleet


def trust_hospital(sitreps: pd.DataFrame, trust_code: str, before: pd.Timestamp,
                   a: StaffingAssumptions = StaffingAssumptions()) -> HospitalConfig:
    """Assumed hospital for one trust: bed base = most beds open on any day before ``before``.

    ICU nurses may float to G&A wards; there is no patient overflow between the two
    (a ward patient cannot use a critical care bed, and the reverse is unsafe).
    """
    df = sitreps[(sitreps.trust_code == trust_code) & (sitreps.date < before)]
    units = []
    for name in UNITS:
        beds = int(df[df.unit == name].beds_open.max())
        nurses_full = beds / a.patients_per_nurse[name]
        units.append(Unit(
            name, beds=beds, surge_beds=max(1, math.ceil(a.surge_share[name] * beds)),
            patients_per_nurse=a.patients_per_nurse[name], pumps_per_patient=a.pumps_per_patient[name],
            shortage_cost=a.shortage_cost[name], float_ok=name != "ICU",
            agency_max=max(1, math.ceil(a.agency_share[name] * nurses_full)),
        ))
    pool = math.ceil(a.nurse_pool_factor * sum(u.beds / u.patients_per_nurse for u in units))
    fleet = round(a.pump_fleet_factor * sum(u.beds * u.pumps_per_patient for u in units))
    return HospitalConfig(
        units=tuple(units), nurse_moves=(("ICU", "G&A"),), nurse_pool_max=pool,
        float_pool=max(1, math.ceil(a.float_share * pool)), pump_fleet=fleet,
        pump_moves_max=max(1, math.ceil(a.pump_move_share * fleet)),
    )


@dataclass
class SitrepRunConfig:
    trusts: tuple[str, ...] = DEFAULT_TRUSTS
    horizon: int = 7
    n_scenarios: int = 40
    seed: int = 0
    scales: tuple[float, ...] = (0.75, 1.0, 1.25, 1.5)
    workers: int = 4
    settings: PlanSettings = field(default_factory=PlanSettings)
    assumptions: StaffingAssumptions = field(default_factory=StaffingAssumptions)


def run_trust(sitreps: pd.DataFrame, trust: str, cfg: SitrepRunConfig, log=print) -> dict:
    """Forecast metrics, forecaster selection and the method comparison for one trust."""
    from .forecast.models import default_forecasters

    series = sitrep_series(sitreps, trust, UNITS)
    if len(series.segments()) < 4:
        raise ValueError(f"{trust}: need at least four winters of sitreps, found {len(series.segments())}")
    phases = season_phases(series, cfg.horizon)
    hospital = trust_hospital(sitreps, trust, series.dates[phases["test"].evaluate[0]], cfg.assumptions)
    pfs = {}
    for f in default_forecasters(census_only=True):
        for ph in ("select", "test"):
            pfs[f.name, ph] = forecast_period(series, f, phases[ph], cfg.horizon, cfg.n_scenarios, cfg.seed)
    fmetrics = pd.DataFrame([forecast_metrics(pf, series) for pf in pfs.values()])
    # per-unit error on the test winter
    per_unit = []
    for (name, ph), pf in pfs.items():
        for j, u in enumerate(series.units):
            err = pf.point[:, j] - pf.actual[:, j]
            per_unit.append({"forecaster": name, "phase": ph, "unit": u, "mae": float(np.abs(err).mean()),
                             "mape_pct": float(100 * np.abs(err).sum() / pf.actual[:, j].sum())})
    any_name = next(iter(pfs))[0]
    oracles = {ph: oracle_costs(hospital, pfs[any_name, ph], cfg.workers) for ph in ("select", "test")}
    log(f"  {trust}: selection ...")
    sel, sel_weekly = selection_experiment(hospital, pfs, oracles, cfg, {"select": series, "test": series})
    choice = choose_configs(sel)
    log(f"  {trust}: method comparison ...")
    f_mae = choice["mae"][0]
    f_reg, s_reg = choice["regret"]
    comp = backtest(hospital, pfs[f_mae, "test"], METHODS, cfg.settings, workers=cfg.workers, label="test")
    extra = backtest(hospital, pfs[f_reg, "test"], ["joint_cvar"], cfg.settings, scale=s_reg, workers=cfg.workers,
                     label="test", oracle_df=oracles["test"])
    extra["method"] = "joint_cvar_decision_selected"
    comp = pd.concat([comp, extra], ignore_index=True)
    for df in (fmetrics, sel, sel_weekly, comp):
        df.insert(0, "trust", trust)
    per_unit = pd.DataFrame(per_unit)
    per_unit.insert(0, "trust", trust)
    seasons = [(str(series.dates[a].date()), str(series.dates[b].date())) for a, b in series.segments()]
    meta = {
        "provenance": series.provenance,
        "hospital": asdict(hospital),
        "seasons": seasons,
        "phases": {k: {"fit_end": str(series.dates[v.fit_end].date()),
                       "calibration": [str(series.dates[v.calib[0]].date()), str(series.dates[v.calib[-1]].date())],
                       "calibration_origins": len(v.calib),
                       "evaluation": [str(series.dates[v.evaluate[0]].date()), str(series.dates[v.evaluate[-1]].date())],
                       "evaluation_weeks": len(v.evaluate)} for k, v in phases.items()},
        "choice": choice,
        "occupancy": occupancy_summary(sitreps, trust),
    }
    return {"series": series, "fmetrics": fmetrics, "per_unit": per_unit, "sel": sel, "sel_weekly": sel_weekly,
            "comp": comp, "meta": meta}


def occupancy_summary(sitreps: pd.DataFrame, trust: str) -> list[dict]:
    df = sitreps[sitreps.trust_code == trust].copy()
    df["season"] = np.where(df.date.dt.month >= 7, df.date.dt.year, df.date.dt.year - 1)
    df["season"] = df.season.astype(str) + "-" + (df.season + 1).astype(str).str[-2:]
    g = df.groupby(["season", "unit"]).agg(days=("date", "nunique"), mean_open=("beds_open", "mean"),
                                           mean_occupied=("beds_occupied", "mean"), max_occupied=("beds_occupied", "max"))
    g["mean_occupancy_pct"] = 100 * g.mean_occupied / g.mean_open
    return g.reset_index().round(1).to_dict("records")


def run_sitrep(files, out_dir: str | Path = "results/nhs_sitrep", cfg: SitrepRunConfig | None = None, log=print) -> dict:
    cfg = cfg or SitrepRunConfig()
    out = Path(out_dir)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    (out / "data").mkdir(exist_ok=True)
    t0 = time.time()
    log("reading sitrep workbooks ...")
    sitreps = clean_sitreps(load_uec_sitreps(sorted(map(str, files))))
    results = {}
    for trust in cfg.trusts:
        log(f"trust {trust} ... ({time.time() - t0:.0f}s)")
        results[trust] = run_trust(sitreps, trust, cfg, log)
        # tidy census-only CSV in the format of docs/data.md (admissions and discharges left empty)
        tidy = results[trust]["series"].to_frame()
        beds = sitreps[sitreps.trust_code == trust].set_index(["date", "unit"]).beds_open
        tidy["beds_open"] = beds.reindex(pd.MultiIndex.from_frame(tidy[["date", "unit"]])).to_numpy()
        tidy.to_csv(out / "data" / f"{trust}.csv", index=False)

    def cat(key):
        return pd.concat([r[key] for r in results.values()], ignore_index=True)

    fmetrics, per_unit, sel, sel_weekly, comp = (cat(k) for k in ("fmetrics", "per_unit", "sel", "sel_weekly", "comp"))
    fmetrics.to_csv(out / "forecast_metrics.csv", index=False)
    per_unit.to_csv(out / "forecast_metrics_by_unit.csv", index=False)
    sel.to_csv(out / "selection.csv", index=False)
    sel_weekly.to_csv(out / "selection_weekly.csv", index=False)
    comp.to_csv(out / "comparison_weekly.csv", index=False)
    comp_summary = summarise(comp, by=("trust", "method"))
    comp_summary.to_csv(out / "comparison_summary.csv", index=False)
    meta = {
        "data": {
            "kind": "real occupancy (NHS England UEC daily sitreps); staffing, pumps and costs assumed",
            "source": "NHS England, Urgent and Emergency Care Daily Situation Reports, Web File Timeseries",
            "files": {k: v for k, v in SITREP_FILES.items()},
            "licence": "Open Government Licence v3.0",
        },
        "assumptions": asdict(cfg.assumptions),
        "run_config": {**asdict(cfg), "settings": asdict(cfg.settings)},
        "trusts": {t: r["meta"] for t, r in results.items()},
        "runtime_seconds": round(time.time() - t0, 1),
    }
    (out / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    write_sitrep_report(out, results, fmetrics, per_unit, sel, comp, comp_summary, meta)
    log(f"done in {time.time() - t0:.0f}s -> {out}")
    return meta


# Report -----------------------------------------------------------------------------------------


def _fig_occupancy(results: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    trusts = list(results)
    fig, axes = plt.subplots(len(trusts), 2, figsize=(10, 2.3 * len(trusts)), squeeze=False)
    for i, t in enumerate(trusts):
        s: DailySeries = results[t]["series"]
        name = s.provenance["trust_name"]
        for j, unit in enumerate(s.units):
            ax = axes[i, j]
            for a, b in s.segments():
                ax.plot(s.dates[a: b + 1], s.census[a: b + 1, j], color="#2a78d6", lw=1)
            ax.set_title(f"{name}: {'adult critical care' if unit == 'ICU' else 'adult G&A'}", fontsize=8, loc="left")
            ax.xaxis.set_major_locator(mdates.YearLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax.tick_params(labelsize=7)
            ax.spines[["top", "right"]].set_visible(False)
            if j == 0:
                ax.set_ylabel("beds occupied", fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def season_name(date) -> str:
    """Winter label such as '2024-25' for a date in that winter."""
    d = pd.Timestamp(date)
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _fig_regret(comp_summary: pd.DataFrame, labels: dict, season: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    order = [m for m in labels if m != "oracle" and m in set(comp_summary.method)]
    trusts = list(dict.fromkeys(comp_summary.trust))
    colors = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")  # fixed categorical order, one per trust
    fig, ax = plt.subplots(figsize=(8, 4.4))
    y = np.arange(len(order))
    width = 0.8 / len(trusts)
    for i, t in enumerate(trusts):
        s = comp_summary[comp_summary.trust == t].set_index("method").reindex(order)
        ax.barh(y + (i - (len(trusts) - 1) / 2) * width, s["regret_pct"], height=width * 0.95,
                color=colors[i % len(colors)], label=t)
    ax.set_yticks(y, [labels[m] for m in order])
    ax.invert_yaxis()
    ax.set_xlabel(f"Regret: % above perfect-foresight cost on the {season} winter (lower is better)")
    ax.legend(frameon=False, loc="lower right", title="trust")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_sitrep_report(out: Path, results, fmetrics, per_unit, sel, comp, comp_summary, meta) -> None:
    from .report import METHOD_LABELS, md_table

    out = Path(out)
    _fig_occupancy(results, out / "figures" / "occupancy.png")
    trusts = list(results)
    first = meta["trusts"][trusts[0]]
    test, select = first["phases"]["test"], first["phases"]["select"]
    names = [season_name(a) for a, _ in first["seasons"]]
    _fig_regret(comp_summary, METHOD_LABELS, names[-1], out / "figures" / "regret_by_method.png")

    lines = [
        "# Results on real NHS bed occupancy (UEC daily sitreps)",
        "",
        "> **Data notice.** Demand here is **real**: adult general and acute (G&A) and adult critical care beds occupied, "
        "per trust per day, from NHS England's Urgent and Emergency Care Daily Situation Reports (8am snapshot; "
        "unvalidated management information; Open Government Licence v3.0). Everything else is **assumed**: the bed base "
        "is the most beds the trust opened in earlier winters, and nurse ratios, nurse pools, agency limits, infusion "
        "pumps and all costs come from `StaffingAssumptions` in `hcalloc/sitrep.py`, not from the trusts. "
        "Regret and cost figures therefore show how the methods behave on real demand patterns, not what any trust "
        "would save.",
        "",
        f"Seasons: {', '.join(f'{a} to {b}' for a, b in first['seasons'])}. "
        f"Selection: fit on {', '.join(names[:-3])}, calibrate scenarios on {names[-3]}, evaluate "
        f"{select['evaluation_weeks']} weekly plans in {names[-2]}. "
        f"Test: fit on {', '.join(names[:-2])}, calibrate on {names[-2]}, evaluate {test['evaluation_weeks']} weekly plans in {names[-1]} "
        f"({test['evaluation'][0]} to {test['evaluation'][1]}). The first 4 weeks of each winter are history only. "
        "Plans are made each Sunday for Monday to Sunday, with two units (adult critical care as `ICU`, adult G&A as `G&A`). "
        "Sitreps publish census only, so the flow forecaster is not used. "
        f"Run time {meta['runtime_seconds']:.0f} s.",
        "",
        "![Occupancy](figures/occupancy.png)",
        "",
        "## Trusts and data",
        "",
    ]
    rows = []
    for t in trusts:
        m = meta["trusts"][t]
        h = m["hospital"]
        units = {u["name"]: u for u in h["units"]}
        rows.append({"trust": t, "name": m["provenance"]["trust_name"],
                     "G&A bed base": units["G&A"]["beds"], "CC bed base": units["ICU"]["beds"],
                     "nurse pool (assumed)": h["nurse_pool_max"], "pump fleet (assumed)": h["pump_fleet"],
                     "days used": int(results[t]["series"].observed.sum()),
                     "values blanked as errors": m["provenance"]["values_cleaned"],
                     "days interpolated": m["provenance"]["days_filled"]})
    lines += [md_table(pd.DataFrame(rows), 0), "", "Mean occupancy by winter (beds occupied / beds open):", ""]
    occ = pd.DataFrame([dict(trust=t, **r) for t in trusts for r in meta["trusts"][t]["occupancy"]])
    occ = occ.pivot_table(index=["trust", "season"], columns="unit", values="mean_occupancy_pct").reset_index()
    occ.columns = [f"{c} occupancy %" if c in UNITS else c for c in occ.columns]
    lines += [md_table(occ, 1), ""]

    # Forecasts
    fm = fmetrics[fmetrics.phase == "test"][["trust", "forecaster", "mae", "mase_vs_naive", "crps", "coverage80", "coverage90"]]
    pu = per_unit[per_unit.phase == "test"].pivot_table(index=["trust", "forecaster"], columns="unit", values="mape_pct").reset_index()
    pu.columns = [f"{c} error %" if c in UNITS else c for c in pu.columns]
    fm = fm.merge(pu, on=["trust", "forecaster"])
    lines += [f"## 1. Forecast accuracy on the {names[-1]} winter", "",
              "Census 1 to 7 days ahead, pooled over both units. MASE below 1 beats last week's value; coverage is the share "
              "of actual values inside the central 80% and 90% of the conformal scenarios. Error % is total absolute error "
              "over total occupied beds, per unit.", "", md_table(fm, 2), ""]

    # Selection
    ch = pd.DataFrame([{"trust": t, "lowest MAE": meta["trusts"][t]["choice"]["mae"][0],
                        "lowest CRPS": meta["trusts"][t]["choice"]["crps"][0],
                        "lowest regret (forecaster, scale)": "{} ({})".format(*meta["trusts"][t]["choice"]["regret"]),
                        "Spearman MAE vs regret": meta["trusts"][t]["choice"]["spearman_mae_vs_regret"],
                        "Spearman CRPS vs regret": meta["trusts"][t]["choice"]["spearman_crps_vs_regret"]} for t in trusts])
    lines += ["## 2. Choosing the forecaster by decisions", "",
              f"Each forecaster and scenario scale is scored on the {names[-2]} winter by the regret of the joint + CVaR plans it "
              "produces. With 4 forecasters the rank correlations are coarse.", "", md_table(ch, 2), ""]

    # Comparison
    keep = ["method", "mean_weekly_cost", "regret_pct", "cvar90_weekly_cost", "shortage_patient_days",
            "shortage_unit_day_rate_pct", "idle_bed_days", "agency_nurse_days"]
    lines += [f"## 3. Method comparison on the {names[-1]} winter", "",
              "Forecasts for every method come from each trust's lowest-validation-MAE forecaster, except the last row "
              "(configuration chosen by validation regret). Costs are in assumed units of one rostered nurse-day.", "",
              "![Regret by method](figures/regret_by_method.png)", ""]
    pooled = []
    for t in trusts:
        s = comp_summary[comp_summary.trust == t][keep].copy()
        s["method"] = s["method"].map(METHOD_LABELS)
        lines += [f"### {t}: {meta['trusts'][t]['provenance']['trust_name']}", "", md_table(s), ""]
    # pooled paired comparison: weekly regret as % of that week's oracle cost, so trusts of different size weigh equally
    comp = comp.copy()
    comp["regret_rel"] = 100 * comp.regret / comp.oracle_cost
    w = comp.pivot_table(index=["trust", "week"], columns="method", values="regret_rel")
    for m in ("rule", "deterministic", "quantile", "sequential", "joint"):
        d, lo, hi = paired_bootstrap(w["joint_cvar"].to_numpy(), w[m].to_numpy())
        pooled.append({"compared with": METHOD_LABELS[m], "regret points difference": d, "95% CI low": lo,
                       "95% CI high": hi, "weeks": len(w)})
    lines += ["### All trusts pooled", "",
              "Joint + CVaR minus each alternative, in weekly regret as % of that week's perfect-foresight cost "
              "(negative means joint + CVaR is better). Paired bootstrap over trust-weeks.", "",
              md_table(pd.DataFrame(pooled), 2), ""]

    lines += ["## Findings", "", *_sitrep_findings(comp_summary, pd.DataFrame(pooled), fmetrics, meta, trusts), ""]
    lines += ["## Limitations", "",
              "- Only demand is real. Staffing ratios, nurse pools, agency limits, pumps and costs are assumptions scaled "
              "to each trust's bed numbers; real rosters, pump fleets and cost weights would change the regret figures.",
              "- Sitreps are winter-only (roughly late November to March), so each model is fitted on one or two short "
              f"winters, and the test is one winter of {test['evaluation_weeks']} weekly plans per trust. Intervals are wide.",
              "- Census is an 8am snapshot of occupied beds, not a midnight count, and is unvalidated management "
              "information that trusts can revise. Obvious reporting errors (zeros on one day, a fourfold jump) are blanked "
              "by `clean_sitreps` and gaps of up to 3 days interpolated.",
              "- Observed occupancy is already capped by the beds each trust opened; unmet demand (patients waiting in "
              "A&E for a bed) is not in these figures, so true demand at full trusts is understated.",
              f"- {names[0]} is used only for fitting{' (it includes the Omicron wave)' if names[0] == '2021-22' else ''}.",
              "- Three trusts, picked for size spread and complete data, not at random.",
              ""]
    (out / "REPORT.md").write_text("\n".join(lines))


def _sitrep_findings(comp_summary, pooled, fmetrics, meta, trusts) -> list[str]:
    from .report import METHOD_LABELS

    out = []
    t = comp_summary.set_index(["trust", "method"])
    wins = []
    for tr in trusts:
        simple = min(("rule", "deterministic", "quantile"), key=lambda m: t.loc[(tr, m), "regret_pct"])
        wins.append((tr, t.loc[(tr, "joint_cvar"), "regret_pct"], t.loc[(tr, simple), "regret_pct"], simple))
    n_win = sum(jc < bs for _, jc, bs, _ in wins)
    out.append(f"- **Uncertainty-aware planning:** joint + CVaR had lower regret than the best method that ignores "
               f"uncertainty in {n_win} of {len(trusts)} trusts ("
               + "; ".join(f"{tr}: {jc:.1f}% vs {bs:.1f}% for {METHOD_LABELS[m].lower()}" for tr, jc, bs, m in wins) + ").")
    p = pooled.set_index("compared with")
    for m in ("rule", "deterministic", "quantile", "sequential", "joint"):
        r = p.loc[METHOD_LABELS[m]]
        verdict = ("no measurable difference" if r["95% CI low"] <= 0 <= r["95% CI high"]
                   else "joint + CVaR better" if r["95% CI high"] < 0 else "joint + CVaR worse")
        out.append(f"- **Joint + CVaR vs {METHOD_LABELS[m].lower()}, pooled:** {r['regret points difference']:+.2f} regret "
                   f"points per week (95% CI {r['95% CI low']:+.2f} to {r['95% CI high']:+.2f}): {verdict}.")
    ft = fmetrics[fmetrics.phase == "test"]
    best = ft.loc[ft.groupby("trust").mase_vs_naive.idxmin()]
    out.append("- **Forecasting:** best test-winter MASE per trust: "
               + ", ".join(f"{r.trust} `{r.forecaster}` {r.mase_vs_naive:.2f} (90% coverage {r.coverage90:.2f})"
                           for r in best.itertuples()) + ".")
    return out
