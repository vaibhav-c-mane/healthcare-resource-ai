"""Write figures and REPORT.md from experiment outputs. Wording is generated from the numbers."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .evaluate.backtest import paired_bootstrap  # noqa: E402

METHOD_LABELS = {
    "rule": "Occupancy rule (85%)",
    "deterministic": "Point forecast + optimiser",
    "quantile": "80th percentile + optimiser",
    "sequential": "Stochastic, sequential",
    "joint": "Stochastic, joint (expected cost)",
    "joint_cvar": "Stochastic, joint + CVaR",
    "joint_cvar_decision_selected": "Joint + CVaR, forecaster chosen by regret",
    "oracle": "Perfect foresight",
}


def md_table(df: pd.DataFrame, digits: int = 1) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            cells.append(f"{v:,.{digits}f}" if isinstance(v, (float, np.floating)) else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _fig_regret(summary: pd.DataFrame, path: Path) -> None:
    order = [m for m in METHOD_LABELS if m != "oracle" and m in set(summary.method)]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    y = np.arange(len(order))
    for i, (label, color) in enumerate((("test", "#3b6ea8"), ("stress", "#c8553d"))):
        s = summary[summary.label == label].set_index("method").reindex(order)
        ax.barh(y + (i - 0.5) * 0.38, s["regret_pct"], height=0.36, color=color,
                label="normal year" if label == "test" else "stress wave year")
    ax.set_yticks(y, [METHOD_LABELS[m] for m in order])
    ax.invert_yaxis()
    ax.set_xlabel("Regret: % above perfect-foresight cost (lower is better)")
    ax.legend(frameon=False, loc="lower right")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_selection(sel: pd.DataFrame, path: Path) -> None:
    s = sel[sel.phase == "select"]
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    for name, g in s.groupby("forecaster"):
        ax.plot(g["mae"], g["mean_weekly_regret"], "o", alpha=0.35, color="#777")
        b = g[g.scale == 1.0].iloc[0]
        ax.plot(b["mae"], b["mean_weekly_regret"], "o", color="#3b6ea8")
        offset = (6, -12) if name == "negbin_glm" else (6, 4)
        ax.annotate(name, (b["mae"], b["mean_weekly_regret"]), textcoords="offset points", xytext=offset, fontsize=9)
    ax.set_xlabel("Forecast MAE (patients per unit-day)")
    ax.set_ylabel("Mean weekly regret (cost units)")
    ax.set_title("Validation year: accuracy vs decision quality\n(blue = conformal scale 1.0, grey = other scales)", fontsize=10)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _fig_overrides(ovr: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 3.8))
    wk = ovr.week + 1
    for col, color, label in (("override_l1_static", "#999", "without learning (static weights)"),
                              ("override_l1_learned", "#3b6ea8", "with override learning"),
                              ("noise_floor_l1", "#c8553d", "administrator's own habitual noise")):
        ax.plot(wk, ovr[col], color=color, alpha=0.25, lw=1)
        ax.plot(wk, ovr[col].rolling(4, min_periods=1).mean(), color=color, lw=2, label=label)
    ax.set_xlabel("Week of test year")
    ax.set_ylabel("Override size (beds + nurses changed)\n4-week average, weekly values faint")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_report(out: Path, fmetrics, sel, choice, comp, comp_summary, ovr, meta, sel_weekly=None, sens=None) -> None:
    out = Path(out)
    fig = out / "figures"
    fig.mkdir(exist_ok=True)
    _fig_regret(comp_summary, fig / "regret_by_method.png")
    _fig_selection(sel, fig / "accuracy_vs_regret.png")
    _fig_overrides(ovr, fig / "override_learning.png")

    lines = [
        "# Results: forecast-then-allocate on semi-synthetic data",
        "",
        "> **Data notice.** Every number below comes from the semi-synthetic hospital simulator in "
        "`hcalloc.data.synthetic` (seed {seed}). Staffing, equipment and administrator behaviour are simulated. "
        "These results test the method; they are not evidence about any real hospital.".format(seed=meta["data"]["normal"]["seed"]),
        "",
        f"Run time: {meta['runtime_seconds']:.0f} s. Evaluation: one plan every Sunday for Monday to Sunday, "
        f"{meta['phases']['test']['evaluation_origins']} weeks in the test year. Costs are in units of one rostered nurse-day. "
        "Regret is realised cost minus the cost of the best plan made with perfect foresight.",
        "",
        "## Key findings",
        "",
        *_findings(comp, comp_summary, sel, choice, ovr, meta, sel_weekly, sens),
        "",
    ]

    # Experiment 1
    keep = ["method", "mean_weekly_cost", "regret_pct", "cvar90_weekly_cost", "shortage_patient_days",
            "shortage_unit_day_rate_pct", "idle_bed_days", "excess_nurse_days", "agency_nurse_days"]
    lines += ["## 1. Method comparison", "",
              f"Forecasts for every method come from the forecaster with the lowest validation MAE (`{choice['mae'][0]}`), "
              "except the last row, which uses the configuration chosen by validation regret "
              f"(`{choice['regret'][0]}`, scenario scale {choice['regret'][1]}).", "",
              "![Regret by method](figures/regret_by_method.png)", ""]
    for label, title in (("test", "Normal test year"), ("stress", "Test year with a pandemic-like stress wave")):
        s = comp_summary[comp_summary.label == label][keep].copy()
        s["method"] = s["method"].map(METHOD_LABELS)
        lines += [f"### {title}", "", md_table(s), ""]
        w = comp[comp.label == label].pivot_table(index="week", columns="method", values="total_cost")
        ci = []
        for m in ("rule", "deterministic", "quantile", "sequential", "joint"):
            if m in w and "joint_cvar" in w:
                d, lo, hi = paired_bootstrap(w["joint_cvar"].to_numpy(), w[m].to_numpy())
                ci.append({"compared with": METHOD_LABELS[m], "weekly cost difference": d, "95% CI low": lo, "95% CI high": hi})
        if ci:
            lines += ["Joint + CVaR minus each alternative, per week (negative means joint + CVaR is cheaper):", "",
                      md_table(pd.DataFrame(ci), 2), ""]

    # Experiment 2
    base = sel[sel.scale == 1.0][["phase", "forecaster", "mae", "crps", "coverage90", "mean_weekly_regret", "regret_pct"]]
    lines += ["## 2. Choosing the forecaster by decisions, not error", "",
              "![Accuracy vs regret](figures/accuracy_vs_regret.png)", "",
              f"On the validation year, lowest MAE picks `{choice['mae'][0]}`, lowest CRPS picks `{choice['crps'][0]}`, and lowest "
              f"decision regret picks `{choice['regret'][0]}` with scenario scale {choice['regret'][1]}. "
              f"Spearman rank correlation between MAE and regret across forecasters: {choice['spearman_mae_vs_regret']:.2f} "
              f"(CRPS vs regret: {choice['spearman_crps_vs_regret']:.2f}).", "",
              md_table(base, 3), ""]
    test = sel[sel.phase == "test"].set_index(["forecaster", "scale"])
    mae_cfg, reg_cfg = choice["mae"], choice["regret"]
    r_mae = test.loc[mae_cfg, "mean_weekly_regret"]
    r_reg = test.loc[reg_cfg, "mean_weekly_regret"]
    best_test = test["mean_weekly_regret"].idxmin()
    lines += [f"On the held-out test year the MAE-selected configuration has mean weekly regret {r_mae:.1f} and the "
              f"regret-selected one {r_reg:.1f} ({'lower' if r_reg < r_mae else 'not lower'}). The best configuration in "
              f"hindsight on the test year is `{best_test[0]}` at scale {best_test[1]} "
              f"({test['mean_weekly_regret'].min():.1f}).", ""]

    # Experiment 3
    n = len(ovr)
    head, tail = ovr.iloc[: min(8, n)], ovr.iloc[max(0, n - 8):]
    true = meta["true_admin_theta"]
    final_theta = (ovr.learned_multiplier.iloc[-1], ovr.learned_risk_weight.iloc[-1])
    lines += ["## 3. Learning from administrator overrides", "",
              f"A simulated administrator plans with hidden weights (shortage multiplier {true[0]}, risk weight {true[1]}) "
              "and adds small habitual changes. The learner searches a bounded grid of weights by decision matching.", "",
              "![Override learning](figures/override_learning.png)", "",
              md_table(pd.DataFrame([
                  {"period": "first 8 weeks", "override size, static": head.override_l1_static.mean(),
                   "override size, learned": head.override_l1_learned.mean(),
                   "share changed, static": head.override_share_static.mean(),
                   "share changed, learned": head.override_share_learned.mean(),
                   "noise floor": head.noise_floor_l1.mean()},
                  {"period": "last 8 weeks", "override size, static": tail.override_l1_static.mean(),
                   "override size, learned": tail.override_l1_learned.mean(),
                   "share changed, static": tail.override_share_static.mean(),
                   "share changed, learned": tail.override_share_learned.mean(),
                   "noise floor": tail.noise_floor_l1.mean()},
              ]), 2), "",
              f"Final learned weights: shortage multiplier {final_theta[0]}, risk weight {final_theta[1]} "
              f"(true values {true[0]}, {true[1]}; the grid holds 1.0 to 3.0 and 0, 0.3, 0.6). "
              f"Realised cost over the year: static recommendations {ovr.cost_static.sum():,.0f}, learned "
              f"{ovr.cost_learned.sum():,.0f}, administrator's final plans {ovr.cost_admin_final.sum():,.0f}. "
              "A learned recommendation can cost more under the configured costs, because it adopts the administrator's "
              "stronger aversion to shortage; shortage patient-days: static "
              f"{ovr.shortage_static.sum():,.0f}, learned {ovr.shortage_learned.sum():,.0f}.", ""]

    if sens is not None:
        piv = sens.pivot_table(index="label", columns="method", values="regret_pct", sort=False)
        piv = piv.rename(columns=METHOD_LABELS).reset_index().rename(columns={"label": "variant"})
        lines += ["## 4. Sensitivity to assumptions", "",
                  "Regret (% above perfect foresight) on the normal test year when one assumption changes at a time "
                  "(forecaster `flow`). Each variant uses its own costs for both planning and scoring.", "",
                  md_table(piv, 1), ""]

    lines += ["## Limitations", "",
              "- Demand, staffing ratios, equipment needs, costs and administrator behaviour are simulated or assumed; see "
              "`docs/assumptions.md`.",
              "- Days are planned independently given first-stage decisions, unserved patients do not carry over, and patients "
              "cannot overflow into another unit's beds.",
              "- One synthetic hospital and one seed; repeat with other seeds and with real aggregate data before drawing conclusions.",
              ""]
    (out / "REPORT.md").write_text("\n".join(lines))


def _ci_text(d: float, lo: float, hi: float) -> str:
    return f"{d:+.1f} per week, 95% CI {lo:+.1f} to {hi:+.1f}"


def _findings(comp, comp_summary, sel, choice, ovr, meta, sel_weekly, sens) -> list[str]:
    out = []
    t = comp_summary.set_index(["label", "method"])
    for label, name in (("test", "normal year"), ("stress", "stress-wave year")):
        simple = min(("rule", "deterministic", "quantile"), key=lambda m: t.loc[(label, m), "regret_pct"])
        jc, bs = t.loc[(label, "joint_cvar"), "regret_pct"], t.loc[(label, simple), "regret_pct"]
        out.append(f"- **Uncertainty-aware planning, {name}:** joint + CVaR is {jc:.1f}% above perfect foresight, against "
                   f"{bs:.1f}% for the best method that ignores uncertainty ({METHOD_LABELS[simple].lower()}), "
                   f"a {100 * (1 - jc / bs):.0f}% cut in regret.")
    w = comp[comp.label == "test"].pivot_table(index="week", columns="method", values="total_cost")
    d, lo, hi = paired_bootstrap(w["joint_cvar"].to_numpy(), w["sequential"].to_numpy())
    verdict = "was not measurably better than" if lo <= 0 <= hi else ("was cheaper than" if hi < 0 else "was dearer than")
    out.append(f"- **Coupled fixed and movable resources:** planning beds, nurses and pumps together {verdict} planning them "
               f"one after another on the normal year ({_ci_text(d, lo, hi)}). The sensitivity table shows when coupling matters more.")
    d2, lo2, hi2 = paired_bootstrap(w["joint_cvar"].to_numpy(), w["joint"].to_numpy())
    cv_jc, cv_j = t.loc[("test", "joint_cvar"), "cvar90_weekly_cost"], t.loc[("test", "joint"), "cvar90_weekly_cost"]
    cv_jc_s, cv_j_s = t.loc[("stress", "joint_cvar"), "cvar90_weekly_cost"], t.loc[("stress", "joint"), "cvar90_weekly_cost"]
    out.append(f"- **Risk term (CVaR, weight {meta['run_config']['settings']['risk_weight']}):** worst-10%-week cost was "
               f"{cv_jc:,.0f} with CVaR vs {cv_j:,.0f} without (normal year) and {cv_jc_s:,.0f} vs {cv_j_s:,.0f} (stress year). "
               f"Mean weekly cost difference: {_ci_text(d2, lo2, hi2)}. In these runs the risk term "
               f"{'did not clearly reduce bad-week cost' if abs(cv_jc - cv_j) < 0.01 * cv_j or cv_jc >= cv_j else 'reduced bad-week cost'}.")
    sline = (f"- **Choosing forecasters by decisions:** forecaster rankings by CRPS and by decision regret agreed "
             f"(Spearman {choice['spearman_crps_vs_regret']:.2f}); MAE agreed less ({choice['spearman_mae_vs_regret']:.2f}). "
             f"Validation regret picked `{choice['regret'][0]}` (scale {choice['regret'][1]}) and MAE picked `{choice['mae'][0]}`.")
    if sel_weekly is not None:
        tw = sel_weekly[sel_weekly.phase == "test"]
        key = lambda c: f"{c[0]}|{c[1]}"
        a_ = tw[tw.label == key(choice["regret"])].sort_values("week").regret.to_numpy()
        b_ = tw[tw.label == key(choice["mae"])].sort_values("week").regret.to_numpy()
        if len(a_) and len(b_) and key(choice["regret"]) != key(choice["mae"]):
            d3, lo3, hi3 = paired_bootstrap(a_, b_)
            sline += (f" On the test year the regret pick minus the MAE pick was {_ci_text(d3, lo3, hi3)}, so "
                      f"{'the choice made no measurable difference' if lo3 <= 0 <= hi3 else ('the regret pick was better' if hi3 < 0 else 'the MAE pick was better')}.")
    base = sel[(sel.phase == "select")]
    tuned = base.loc[base.groupby("forecaster").mean_weekly_regret.idxmin()]
    widened = tuned[tuned.scale > 1.0]
    if len(widened):
        sline += (" Tuning the scenario spread by regret mattered for the weaker models: "
                  + ", ".join(f"`{r.forecaster}` preferred scale {r.scale} (90% coverage {r.coverage90:.2f})" for r in widened.itertuples())
                  + ", i.e. wider scenarios than plain conformal calibration gave.")
    out.append(sline)
    n = len(ovr)
    tail = ovr.iloc[max(0, n - 8):]
    red = 100 * (1 - tail.override_l1_learned.mean() / tail.override_l1_static.mean())
    true = meta["true_admin_theta"]
    out.append(f"- **Override learning:** over the last 8 weeks, overrides were {red:.0f}% smaller with learning "
               f"({tail.override_l1_learned.mean():.1f} vs {tail.override_l1_static.mean():.1f} beds + nurses changed per week; "
               f"habitual noise alone {tail.noise_floor_l1.mean():.1f}). Learned weights ended at "
               f"({ovr.learned_multiplier.iloc[-1]}, {ovr.learned_risk_weight.iloc[-1]}) for a true ({true[0]}, {true[1]}). "
               "This shows the mechanism works on a simulated administrator only.")
    if sens is not None:
        piv = sens.pivot_table(index="label", columns="method", values="regret_pct", sort=False)
        adv = (piv["deterministic"] - piv["joint_cvar"]).round(1)
        gap = (piv["sequential"] - piv["joint_cvar"]).round(1)
        lead = ("had lower regret than the point-forecast optimiser in every variant"
                if (adv > 0).all() else "did not have lower regret than the point-forecast optimiser in every variant")
        out.append(f"- **Sensitivity:** joint + CVaR {lead}, by {adv.min():.1f} points ({adv.idxmin()}) up to "
                   f"{adv.max():.1f} points ({adv.idxmax()}). Joint minus sequential planning, in regret points saved "
                   "(positive means joint is better): " + ", ".join(f"{k}: {v:+.1f}" for k, v in gap.items())
                   + ". Coupling helps most where a movable resource is scarce or opening beds is expensive.")
    return out


def rebuild(out_dir: str | Path = "results") -> None:
    """Regenerate figures and REPORT.md from the CSVs and metadata already in ``out_dir``."""
    import json

    out = Path(out_dir)
    meta = json.loads((out / "run_metadata.json").read_text())
    meta["choice"]["mae"] = tuple(meta["choice"]["mae"])
    meta["choice"]["regret"] = tuple(meta["choice"]["regret"])
    meta["choice"]["crps"] = tuple(meta["choice"]["crps"])
    sens = pd.read_csv(out / "sensitivity_summary.csv") if (out / "sensitivity_summary.csv").exists() else None
    sel_weekly = pd.read_csv(out / "selection_weekly.csv") if (out / "selection_weekly.csv").exists() else None
    write_report(out, pd.read_csv(out / "forecast_metrics.csv"), pd.read_csv(out / "selection.csv"), meta["choice"],
                 pd.read_csv(out / "comparison_weekly.csv"), pd.read_csv(out / "comparison_summary.csv"),
                 pd.read_csv(out / "overrides.csv"), meta, sel_weekly=sel_weekly, sens=sens)
