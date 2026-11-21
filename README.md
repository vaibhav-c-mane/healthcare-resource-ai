# hcalloc: AI Resource Allocation and Prediction System in Healthcare

Forecast hospital demand as a probability distribution, then use that distribution to decide how many beds to open, how many nurses to roster, and where to place mobile equipment, all in one optimisation. Runs offline on a single CPU machine and uses only daily aggregate counts.

> **Data notice.** The results in `results/` come from a semi-synthetic hospital simulator, not a real hospital. See [docs/data.md](docs/data.md) for how to plug in real data and [docs/assumptions.md](docs/assumptions.md) for every assumed value.

## v2: beds across a network of hospitals

v2 plans **one resource, open beds, for several hospitals at once**. For each hospital it forecasts occupied beds for the next 7 days, keeps a reserve of open beds, and plans patient transfers to other hospitals at a fixed cost and up to a fixed number per day. Nurses and equipment stay in the v1 planner below and are future scope for the network version.

- Code: `src/hcalloc/network/`. Tests: `tests/test_network.py`.
- Bed dashboard and JSON API for the EDI React app: `hcalloc dashboard`, then http://127.0.0.1:8000 (needs `pip install -e ".[dashboard]"`).
- Experiments E1 (policy comparison), E2 (local, simultaneous and near-capacity surges) and E3 (transfer cost and cap sweep), on a SYNTHETIC 3-hospital network and on two real NHS networks (Tyne and Wear; Greater Manchester): `hcalloc network-experiments --sitrep-csv data/sitrep/ga_long.csv`. Results: [results/network/REPORT.md](results/network/REPORT.md).
- Perfect foresight, the regret benchmark, obeys the same beds, transfer cap and costs as every policy.
- Headline results (details and confidence intervals in the report): the transfer network roughly halves unmet bed-days on both NHS networks compared with hospitals that never transfer, and its value shrinks when surges hit all hospitals at once; planning on a point forecast instead of scenarios is far worse; the 85% rule leaves 2 to 5 times more beds idle; and building expected transfers into the plan did not beat planning each hospital alone and transferring on the day.
- How to run on Windows, connect to React, and next steps for the paper: [docs/HANDOVER.md](docs/HANDOVER.md).

## v1: one hospital, beds + nurses + equipment

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
hcalloc sitrep-fetch && hcalloc sitrep                       # same engine on real NHS bed occupancy (about 2 minutes)
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
results/nhs_sitrep/      outputs of `hcalloc sitrep` (real NHS occupancy, assumed staffing and costs)
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

## Results on real NHS bed occupancy

Three English trusts (Torbay and South Devon, Dudley, Newcastle upon Tyne), adult G&A and adult critical care beds occupied each day from NHS England's UEC daily sitreps, four winters 2021-22 to 2024-25; tested on 14 weekly plans in winter 2024-25. **Demand is real; staffing ratios, nurse pools, pumps and costs are assumed.** Full report: [results/nhs_sitrep/REPORT.md](results/nhs_sitrep/REPORT.md).

- Stochastic planning beat the 85% occupancy rule clearly (7.3 regret points lower per week, 95% CI 6.4 to 8.4, pooled over trusts).
- Against planning on a point forecast or on the 80th percentile, the advantage seen on synthetic data did not show up: differences were within about a point and not statistically clear on 42 trust-weeks.
- The CVaR term cost slightly more than planning for expected cost (+0.7 points, 95% CI +0.1 to +1.3), and coupled planning was no better than sequential at these assumed staffing levels.
- The best forecaster per trust was off by about 3% for G&A and 6 to 17% for critical care (small units are noisy); Holt-Winters or level-relative gradient boosting beat last week's value at every trust, and conformal 90% intervals covered 84 to 91% of outcomes.

## Status and next steps

v1.1 implements the plan of record end to end on synthetic data, with patient overflow between units and a five-hospital replication. It has also been run on real NHS occupancy (above). Next: real staffing and cost data from a partner hospital, MIMIC-IV flows, widen the cost sweep, and pilot the app in advisory mode.
