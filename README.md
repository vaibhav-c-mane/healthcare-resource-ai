# hcalloc: AI Resource Allocation and Prediction System in Healthcare

Forecast hospital demand as a probability distribution, then use that distribution to decide how many beds to open, how many nurses to roster, and where to place mobile equipment, all in one optimisation. Runs offline on a single CPU machine and uses only daily aggregate counts.

> **Data notice.** The results in `results/` come from a semi-synthetic hospital simulator, not a real hospital. See [docs/data.md](docs/data.md) for how to plug in real data and [docs/assumptions.md](docs/assumptions.md) for every assumed value.

## What it does

1. **Forecast** each unit's census for the next 7 days with one of five models: seasonal naive, Holt-Winters, gradient-boosted trees, negative binomial regression, or a stock-and-flow model of admissions and discharges.
2. **Turn forecasts into scenarios.** Joint forecast errors from the past year (split-conformal residuals) are added to the point forecast, giving 40 demand scenarios that keep correlation across units and days.
3. **Allocate** with a two-stage stochastic mixed-integer program (HiGHS via SciPy):
   - first stage, decided now: beds opened (fixed resource), nurses rostered and pumps placed per unit and day;
   - second stage, per scenario: escalation beds, float-pool and agency nurses, nurse redeployment between compatible units, pump transfers, patients outlying to a unit of the same or higher level of care, and unmet demand;
   - nurse-to-patient ratios are hard constraints; objective is cost plus a CVaR term for bad weeks.
4. **Choose the forecaster by decisions.** Each forecaster and scenario spread is scored by the regret of the plans it produces, not only by MAE.
5. **Learn from overrides.** Administrator changes are logged with a reason; the system re-estimates its shortage-cost and risk weights by matching past decisions (inverse optimisation over a bounded grid) and suggests new weights for a person to approve.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[app,dev]"

hcalloc simulate --out data/synthetic_hospital.csv          # 5 years of SYNTHETIC daily aggregates
hcalloc plan --data data/synthetic_hospital.csv --out plan.csv   # next week's recommendation
hcalloc app                                                  # offline web app on localhost
hcalloc experiments --out results                            # full benchmarks (about 30 minutes on 4 cores)
pytest                                                       # unit tests
```

## Repository layout

```
src/hcalloc/
  config.py              hospital, cost and planning settings
  data/                  daily series format, synthetic simulator, MIMIC-IV loaders, holiday calendar
  forecast/              five forecasters and the conformal scenario generator
  allocate/              LP builder, two-stage stochastic model, baselines
  evaluate/              realised-cost evaluation, rolling backtests, metrics
  overrides/             override learner and simulated administrator
  service.py             planning service and local SQLite decision log
  app/streamlit_app.py   offline planning app
  experiments.py         benchmark experiments: comparison, selection, overrides, sensitivity, replication
  report.py              figures and REPORT.md
docs/                    assumptions and data guide
results/                 outputs of `hcalloc experiments` (synthetic)
tests/                   unit tests
```

## Results so far (semi-synthetic data)

One 52-week test year, a plan every Sunday, scored against perfect foresight; the method comparison is repeated on four more independently simulated hospitals. Full tables, figures and confidence intervals: [results/REPORT.md](results/REPORT.md).

| Claim from the plan | Result |
| --- | --- |
| Forecast-then-allocate with uncertainty beats point forecasts | Yes. Regret 12.3% vs 16.6% for the best method that ignores uncertainty (26% lower); 10.1% vs 13.6% in a stress-wave year. Held in all seven sensitivity variants (smallest margin 0.2 points, when beds cost as much as a nurse) and in 5 of 5 simulated hospitals (mean 10.7% vs 14.1%). |
| Coupling fixed and movable resources matters | Yes, but the gain is small. Joint planning was 2.3 per week cheaper than planning one resource after another (95% CI 0.6 to 4.0), 5.5 per week in the stress year, and better in 5 of 5 hospitals (mean 0.9 regret points). The gain grows to 3.1 points when the pump fleet is tight and disappears when patients cannot outlie to other units. |
| CVaR risk term protects bad weeks | Not shown. Worst-10%-week cost matched the expected-cost plan, and the CVaR term added 0.4 regret points on average across the five hospitals. |
| Choose forecasters by decisions, not error | Partly. CRPS ranked models exactly like regret; MAE nearly so (rank correlation 0.90). Picking by regret did not change the test-year result (+0.2 per week, 95% CI -2.4 to +2.7). Tuning the scenario spread by regret chose wider scenarios for the weakest model. |
| Learn from administrator overrides | Works on a simulated administrator: overrides 43% smaller after learning; recovered weights (2.0, 0.6) near the hidden (1.8, 0.5). Needs real override logs to confirm. |
| Offline, low-cost | A weekly plan takes about 3 seconds on a 4-core CPU; no GPU, no network. |

v1.1 added patient overflow between units. It lowered the headline gain from 41% (v1) to 26%, because without overflow the model overstated shortages for plans that left one unit full while another had spare beds.

## Status and next steps

v1.1 implements the plan of record end to end on synthetic data, with patient overflow between units and a five-hospital replication. Next: run on real aggregate data (NHS England sitreps, MIMIC-IV, or a partner hospital), widen the cost sweep, and pilot the app in advisory mode.
