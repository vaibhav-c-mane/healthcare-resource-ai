"""Command line entry point: ``hcalloc <command>``."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hcalloc", description="Forecast-then-allocate hospital resource planner")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate", help="write a SYNTHETIC hospital dataset (daily aggregates) to CSV")
    s.add_argument("--out", default="data/synthetic_hospital.csv")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--years", type=int, default=5)
    s.add_argument("--stress-start", default=None, help="start date of a pandemic-like stress wave, e.g. 2023-10-01")

    pl = sub.add_parser("plan", help="recommend next week's beds, nurses and pumps")
    pl.add_argument("--data", required=True, help="tidy CSV: date, unit, census, admissions, discharges")
    pl.add_argument("--origin", default=None, help="plan the 7 days after this date (default: last date)")
    pl.add_argument("--forecaster", default="flow")
    pl.add_argument("--risk-weight", type=float, default=0.3)
    pl.add_argument("--shortage-multiplier", type=float, default=1.0)
    pl.add_argument("--scenarios", type=int, default=40)
    pl.add_argument("--out", default=None, help="write the plan table to this CSV")

    e = sub.add_parser("experiments", help="run the benchmark experiments on semi-synthetic data")
    e.add_argument("--out", default="results")
    e.add_argument("--quick", action="store_true", help="8 weeks and 2 scales, for a smoke test")
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--seed", type=int, default=0)

    sf = sub.add_parser("sitrep-fetch", help="download NHS England UEC daily sitrep workbooks (open data)")
    sf.add_argument("--out", default="data/sitrep")

    sr = sub.add_parser("sitrep", help="run the backtest on real NHS bed occupancy (staffing and costs assumed)")
    sr.add_argument("--files", nargs="+", default=None, help="sitrep workbooks (default: data/sitrep/*.xlsx)")
    sr.add_argument("--trusts", nargs="+", default=None, help="trust codes (default: RA9 RNA RTD)")
    sr.add_argument("--out", default="results/nhs_sitrep")
    sr.add_argument("--workers", type=int, default=4)

    r = sub.add_parser("report", help="rebuild figures and REPORT.md from an existing results folder")
    r.add_argument("--out", default="results")

    sub.add_parser("app", help="launch the offline planning app (needs the 'app' extra)")

    ne = sub.add_parser("network-experiments", help="v2: bed reserves and transfers across hospitals (E1-E3)")
    ne.add_argument("--out", default="results/network")
    ne.add_argument("--sitrep-csv", default=None,
                    help="G&A sitrep cache from `hcalloc network-sitrep-cache` (adds the real NHS networks)")
    ne.add_argument("--quick", action="store_true", help="1 seed, 6 weeks, small sweep: a smoke test")
    ne.add_argument("--workers", type=int, default=4)

    nc = sub.add_parser("network-sitrep-cache", help="read the sitrep workbooks once into a G&A CSV cache")
    nc.add_argument("--files", nargs="+", default=None)
    nc.add_argument("--out", default="data/sitrep/ga_long.csv")

    nr = sub.add_parser("network-report", help="write REPORT.md and figures for results/network")
    nr.add_argument("--out", default="results/network")

    db = sub.add_parser("dashboard", help="v2: launch the bed dashboard and JSON API (needs the 'dashboard' extra)")
    db.add_argument("--host", default="127.0.0.1")
    db.add_argument("--port", type=int, default=8000)

    a = p.parse_args(argv)
    if a.cmd == "simulate":
        from .data.synthetic import StressWave, simulate_hospital

        series = simulate_hospital(seed=a.seed, years=a.years, stress=StressWave(a.stress_start) if a.stress_start else None)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        series.to_csv(a.out)
        print(f"wrote {len(series)} days x {len(series.units)} units of SYNTHETIC data to {a.out}")
    elif a.cmd == "plan":
        from .config import PlanSettings
        from .data.series import DailySeries
        from .service import plan_next_week

        series = DailySeries.read_csv(a.data, units=["ICU", "StepDown", "Medical", "Surgical"])
        res = plan_next_week(series, a.origin, a.forecaster,
                             PlanSettings(risk_weight=a.risk_weight, shortage_multiplier=a.shortage_multiplier),
                             n_scenarios=a.scenarios)
        table = res.table()
        print(table.to_string(index=False))
        print(f"\nfirst-stage cost {res.plan.first_stage_cost:,.1f}  expected recourse {res.plan.expected_recourse:,.1f}"
              f"  CVaR90 recourse {res.plan.cvar(0.9):,.1f}")
        if a.out:
            table.to_csv(a.out, index=False)
    elif a.cmd == "experiments":
        from .experiments import RunConfig, run_all

        cfg = RunConfig(seed=a.seed, workers=a.workers)
        if a.quick:
            cfg = RunConfig(seed=a.seed, workers=a.workers, max_weeks=8, scales=(1.0, 1.25))
        run_all(a.out, cfg)
    elif a.cmd == "sitrep-fetch":
        from .sitrep import download_sitreps

        for path in download_sitreps(a.out):
            print(path)
    elif a.cmd == "sitrep":
        from .sitrep import DEFAULT_TRUSTS, SitrepRunConfig, run_sitrep

        files = a.files or sorted(Path("data/sitrep").glob("*.xlsx"))
        if not files:
            print("no sitrep workbooks found; run `hcalloc sitrep-fetch` first", file=sys.stderr)
            return 1
        run_sitrep(files, a.out, SitrepRunConfig(trusts=tuple(a.trusts or DEFAULT_TRUSTS), workers=a.workers))
    elif a.cmd == "report":
        from .report import rebuild

        rebuild(a.out)
        print(f"rebuilt {a.out}/REPORT.md")
    elif a.cmd == "network-experiments":
        from .network.experiments import NetworkRunConfig, run_all as run_network
        from .network.report import write_report

        rc = NetworkRunConfig(workers=a.workers)
        if a.quick:
            rc = NetworkRunConfig(workers=a.workers, seeds=(0,), max_weeks=6, sweep_transfer_cost=(1.5,),
                                  sweep_cap_share=(0.0, 0.02))
        run_network(a.out, rc, ga_csv=a.sitrep_csv)
        write_report(a.out)
        print(f"wrote {a.out}/REPORT.md")
    elif a.cmd == "network-sitrep-cache":
        from .data.loaders import SITREP_SHEETS, clean_sitreps, load_uec_sitreps

        files = a.files or sorted(Path("data/sitrep").glob("*.xlsx"))
        if not files:
            print("no sitrep workbooks found; run `hcalloc sitrep-fetch` first", file=sys.stderr)
            return 1
        ga = clean_sitreps(load_uec_sitreps(files, sheets={"G&A": SITREP_SHEETS["G&A"]}))
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        ga.to_csv(a.out, index=False)
        print(f"wrote {len(ga)} rows to {a.out}")
    elif a.cmd == "network-report":
        from .network.report import write_report

        write_report(a.out)
        print(f"wrote {a.out}/REPORT.md")
    elif a.cmd == "dashboard":
        import uvicorn

        print(f"bed dashboard on http://{a.host}:{a.port}  (JSON at /api/plan)")
        uvicorn.run("hcalloc.network.api:app", host=a.host, port=a.port)
    elif a.cmd == "app":
        app = Path(__file__).parent / "app" / "streamlit_app.py"
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
