"""REPORT.md and figures for the network experiments (results/network)."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

LABELS = {
    "coordinated": "Coordinated (method)",
    "coordinated_indep": "Coordinated, scenarios not joint",
    "point_coordinated": "Coordinated, point forecast",
    "alone_reactive": "Each alone + transfers on the day",
    "isolated": "Each alone, no transfers",
    "rule85": "85% occupancy rule",
    "p80": "80th percentile buffer",
}
ORDER = list(LABELS)

# Written by hand after the 2026-11-22 run; update when the experiments change.
KEY_FINDINGS = """\
1. **The transfer network is worth a lot, and more as the daily cap grows.** Against hospitals that never transfer, the
   coordinated plan saved 21 to 57 cost units per week on the simulated network and 163 to 190 per week on the two NHS
   networks, roughly halving unmet bed-days on NHS data (317 vs 580 in Tyne and Wear, 301 vs 610 in Greater Manchester).
   The saving grows with the transfer cap and shrinks as transfers cost more (E3).
2. **Synchronised surges shrink the value of the network.** With the same surges hitting one hospital at a time the
   network saved 46 per week; when they hit all hospitals together it saved 21 per week, and unmet bed-days rose two- to
   threefold for the forecast-driven policies. Spare beds next door are not there when everyone is busy.
3. **Planning with uncertainty is essential.** The same planner fed only a point forecast had 2 to 8 times more unmet
   bed-days and cost 200 to 260 per week more (simulated) and 490 to 750 per week more (NHS).
4. **The 85% occupancy rule leaves the fewest patients unplaced, but at a high price**: 2 to 5 times the idle open
   bed-days and 6 to 15% regret, against 3 to 8% for the forecast-driven policies. In simultaneous-surge weeks its
   spare beds paid off and it had the lowest cost, which is a further sign that the forecasts under-predict synchronised surges.
5. **Anticipating transfers inside the plan did not beat planning each hospital alone and transferring on the day.**
   The difference was within noise in most settings and on both NHS networks. Under simultaneous surges, planning alone
   was better (by 9 per week overall, 39 per week in surge weeks), because the coordinated plan opens fewer beds and
   counts on neighbours who turn out to be full. Joint scenarios reduce this (they beat independent scenarios by 51 per
   week in simultaneous-surge weeks) but do not remove it.

Taken together: the network and the uncertainty-aware forecast carry most of the value; relying on neighbours in the
plan itself only pays when surges are not synchronised and scenarios capture how hospitals move together. That is the
'when does it help' answer the paper can build on, with the limits below."""
REGIME_TEXT = {
    "baseline": "Baseline (two random local surges a year)",
    "local": "Local surges (one hospital at a time)",
    "simultaneous": "Simultaneous surges (all hospitals together)",
    "near_capacity": "Near capacity (about 95% full all year)",
    "tyne_and_wear": "NHS Tyne and Wear (RTD, RR7, R0B)",
    "greater_manchester": "NHS Greater Manchester (RM3, RMC, RRF)",
}


def _per_year(df: pd.DataFrame, n_seeds: dict) -> pd.DataFrame:
    out = df.copy()
    k = out.label.map(n_seeds).fillna(1)
    for c in ("unmet_bed_days", "idle_bed_days", "transfers", "cost"):
        out[c] = out[c] / k
    return out


def _table(df: pd.DataFrame, label: str) -> str:
    g = df[df.label == label].set_index("policy").reindex([p for p in ORDER if p in set(df.policy)])
    lines = ["| Policy | Unmet bed-days | Idle open bed-days | Transfers | Regret % | Cost vs method per week (95% CI) |",
             "| --- | ---: | ---: | ---: | ---: | --- |"]
    for pol, r in g.iterrows():
        ci = "reference" if pol == "coordinated" else (
            f"{r.cost_vs_method_per_week:+.1f} ({r.cost_vs_method_lo:+.1f} to {r.cost_vs_method_hi:+.1f})")
        lines.append(f"| {LABELS[pol]} | {r.unmet_bed_days:,.0f} | {r.idle_bed_days:,.0f} | {r.transfers:,.0f} | "
                     f"{r.regret_pct:.1f} | {ci} |")
    return "\n".join(lines)


def _bars(df: pd.DataFrame, labels: list[str], path: Path, title: str) -> None:
    pols = [p for p in ORDER if p in set(df.policy)]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=False)
    width = 0.8 / len(pols)
    x = np.arange(len(labels))
    for ax, col, name in zip(axes, ("unmet_bed_days", "idle_bed_days"), ("Unmet bed-days", "Idle open bed-days")):
        for i, p in enumerate(pols):
            vals = [float(df[(df.label == l) & (df.policy == p)][col].sum()) for l in labels]
            ax.bar(x + (i - len(pols) / 2 + 0.5) * width, vals, width, label=LABELS[p])
        ax.set_xticks(x, [REGIME_TEXT.get(l, l).split(" (")[0].replace(" ", "\n", 1) for l in labels], fontsize=8)
        ax.set_title(name, fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
    axes[1].legend(fontsize=7, frameon=False, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _sweep_fig(sweep: pd.DataFrame, path: Path, title: str) -> pd.DataFrame:
    """Gain of coordination over each-alone-with-transfers, cost per week, by cap and transfer cost."""
    piv = sweep.pivot_table(index=["label", "transfer_cost", "cap_share", "seed", "week"], columns="policy", values="cost")
    piv["gain_vs_alone"] = piv["alone_reactive"] - piv["coordinated"]
    piv["gain_vs_isolated"] = piv["isolated"] - piv["coordinated"]
    g = piv.groupby(["label", "transfer_cost", "cap_share"])[["gain_vs_alone", "gain_vs_isolated"]].mean().reset_index()
    labels = list(dict.fromkeys(g.label))
    fig, axes = plt.subplots(1, len(labels), figsize=(5 * len(labels), 3.6), squeeze=False)
    for ax, l in zip(axes[0], labels):
        for tc, d in g[g.label == l].groupby("transfer_cost"):
            ax.plot(100 * d.cap_share, d.gain_vs_isolated, marker="o", label=f"transfer cost {tc:g}")
        ax.axhline(0, color="grey", lw=0.8)
        ax.set_xlabel("Daily transfer cap (% of beds)")
        ax.set_ylabel("Cost saved per week vs no network")
        ax.set_title(REGIME_TEXT.get(l, l), fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0][-1].legend(fontsize=7, frameon=False)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return g


def write_report(out: str | Path = "results/network") -> Path:
    out = Path(out)
    figs = out / "figures"
    figs.mkdir(exist_ok=True)
    meta = json.loads((out / "run_metadata.json").read_text())
    sim_w = pd.read_csv(out / "sim_weekly.csv")
    n_seeds = sim_w.groupby("label").seed.nunique().to_dict()
    sim = _per_year(pd.read_csv(out / "sim_summary.csv"), n_seeds)
    surge = _per_year(pd.read_csv(out / "sim_surge_weeks_summary.csv"), n_seeds)
    regimes = [r for r in ("baseline", "local", "simultaneous", "near_capacity") if r in set(sim.label)]
    _bars(sim, regimes, figs / "e1_e2_simulated.png", "Simulated network, test year (per seed)")

    parts = [
        "# Network results: bed reserves and transfers across hospitals (hcalloc v2)",
        "",
        "> **Data notice.** The simulated network is SYNTHETIC. The NHS networks use real adult general and acute (G&A) "
        "beds occupied from NHS England daily sitreps (Open Government Licence v3.0); bed counts are the most beds the "
        "trust opened before the test winter. Costs, transfer caps and the 'one transfer = one bed-day' rule are assumptions.",
        "",
        "## Key findings",
        "",
        KEY_FINDINGS,
        "",
        "## Setup",
        "",
        f"- Costs: open bed-day {meta['config']['open_cost']}, transfer {meta['config']['transfer_cost']}, "
        f"unplaced patient-day {meta['config']['unmet_cost']}. Daily transfer cap per sending hospital: "
        f"{100 * meta['config']['cap_share']:.0f}% of its beds.",
        "- Plans: a 7-day plan of open beds per hospital every week of the test period; 40 joint demand scenarios.",
        "- Perfect foresight (the regret benchmark) obeys the same beds, transfer cap and costs as every policy.",
        "- Main measures: unmet bed-days, idle open bed-days, transfers. Regret is secondary.",
        f"- Simulated: {len(set(sim_w.seed))} independent seeds per regime, 52 test weeks each; figures are per seed-year.",
        "",
        "## E1 and E2: simulated network",
        "",
        "![E1 and E2](figures/e1_e2_simulated.png)",
        "",
    ]
    for r in regimes:
        parts += [f"### {REGIME_TEXT[r]}", "", "All test weeks:", "", _table(sim, r), ""]
        if r in set(surge.label):
            parts += ["Surge weeks only:", "", _table(surge, r), ""]

    if (out / "sim_sweep_weekly.csv").exists():
        g = _sweep_fig(pd.read_csv(out / "sim_sweep_weekly.csv"), figs / "e3_sweep_simulated.png",
                       "E3: value of coordination by transfer cap and cost (simulated)")
        g.to_csv(out / "sim_sweep_gain.csv", index=False)
        parts += ["## E3: sensitivity to transfer cost and cap (simulated)", "",
                  "![E3](figures/e3_sweep_simulated.png)", "",
                  "Cost saved per week by the coordinated plan, against each hospital alone with transfers on the day "
                  "(`gain_vs_alone`) and against no network at all (`gain_vs_isolated`):", "",
                  g.round(2).to_markdown(index=False), ""]

    if (out / "nhs_summary.csv").exists():
        nhs = pd.read_csv(out / "nhs_summary.csv")
        peak = pd.read_csv(out / "nhs_peak_weeks_summary.csv")
        nets = list(dict.fromkeys(nhs.label))
        _bars(nhs, nets, figs / "e1_e2_nhs.png", "Real NHS networks, test winter 2024-25")
        parts += ["## E1 and E2: real NHS networks (test winter 2024-25)", "", "![NHS](figures/e1_e2_nhs.png)", ""]
        for m in meta.get("nhs", []):
            parts += [f"### {REGIME_TEXT.get(m['network'], m['network'])}", "",
                      f"Trusts: {', '.join(f'{k} {v}' for k, v in m['trusts'].items())}. Beds: {m['beds']}. "
                      f"Mean test occupancy: {m['mean_test_occupancy']}. Transfer cap per day: {m['transfer_cap_per_day']}. "
                      f"{m['test_weeks']} weeks from {m['test_from']} to {m['test_to']}; forecaster {m['forecaster']} "
                      f"(test MAE {m['test_mae']:.1f} beds per trust-day).", "",
                      "All test weeks:", "", _table(nhs, m["network"]), "",
                      "Peak weeks (busiest quarter of the winter):", "", _table(peak, m["network"]), ""]
        if (out / "nhs_sweep_weekly.csv").exists():
            g = _sweep_fig(pd.read_csv(out / "nhs_sweep_weekly.csv"), figs / "e3_sweep_nhs.png",
                           "E3: value of coordination by transfer cap and cost (NHS)")
            g.to_csv(out / "nhs_sweep_gain.csv", index=False)
            parts += ["### E3 on NHS networks", "", "![E3 NHS](figures/e3_sweep_nhs.png)", "",
                      g.round(2).to_markdown(index=False), ""]

    parts += ["## Files", "",
              "- `sim_weekly.csv`, `nhs_weekly.csv`: one row per week and policy (every measure).",
              "- `*_summary.csv`: totals with paired bootstrap 95% intervals against the coordinated method.",
              "- `*_sweep_*.csv`: E3 sweeps. `run_metadata.json`: settings, forecasters, trust details.", ""]
    path = out / "REPORT.md"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
