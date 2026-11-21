# Handover: hcalloc v2 (beds across hospitals)

This guide is for you and your EDI team to carry on without Claude. It covers what v2 does, how to run it on Windows, how to connect it to the team's React app, how to put it on GitHub, and what to do next for the paper.

## 1. What v2 does, in one paragraph

For 3 hospitals, v2 forecasts occupied beds for the next 7 days, gives a likely range (10th to 90th percentile) from 40 joint scenarios that keep the fact that hospitals get busy together, and solves one optimisation for all hospitals at once. It recommends how many beds each hospital should open each day (the reserve is open beds minus the median forecast). It also says how many patients are likely to move between hospitals, within a fixed daily transfer cap and at a fixed cost per transfer. Every recommendation comes with a plain-language reason. Nurses and equipment are future scope. Override learning and unconfirmed bed updates are optional extras that are not built in v2.

## 2. Run it on Windows (PowerShell in VS Code)

Run **one line at a time** and wait for the prompt to come back before the next line. Use `python -m pip`, not bare `pip`.

```
cd "C:\Users\Admin\OneDrive\Desktop\EDI_final project"
```
Extract `hcalloc-v2.zip` here, then:
```
cd hcalloc
```
```
python -m venv .venv
```
```
.venv\Scripts\Activate.ps1
```
```
python -m pip install -e ".[app,dev,dashboard]"
```
```
pytest
```
All tests should pass (45). Then start the dashboard:
```
hcalloc dashboard
```
Open http://127.0.0.1:8000 in a browser. Pick a date and press Update. Stop it with Ctrl+C.

### Re-run the experiments (optional, about 30 to 60 minutes)

```
hcalloc sitrep-fetch
```
```
hcalloc network-sitrep-cache
```
```
hcalloc network-experiments --sitrep-csv data/sitrep/ga_long.csv
```
Results and figures land in `results/network/`, with a readable summary in `results/network/REPORT.md`. Add `--quick` for a 2-minute smoke test.

## 3. Connect it to the EDI React app

The dashboard server is also a JSON API, so the React app does not need any Python code.

1. Start the API with `hcalloc dashboard` (it listens on port 8000; CORS is open for local development).
2. From React, call:

```js
const res = await fetch("http://127.0.0.1:8000/api/plan?date=2024-02-05");
const plan = await res.json();
```

`GET /api/range` gives the first, last and default dates that can be planned.

The `/api/plan` answer looks like this (shortened):

```json
{
  "planned_on": "2024-02-05",
  "data": "SYNTHETIC network simulator",
  "costs": {"open_bed_day": 1.0, "transfer": 1.5, "unplaced_patient_day": 8.0},
  "hospitals": [
    {"name": "Hospital A", "beds": 150, "transfer_cap_per_day": 3, "occupied_today": 139,
     "days": [{"date": "2024-02-06", "p10": 137.8, "p50": 140.8, "p90": 143.0,
               "open_beds": 143, "reserve": 2, "expected_unplaced": 0.16}],
     "reasons": ["Busiest day Thu 08 Feb: high case 146 beds (97% of beds), so reserves are kept."]}
  ],
  "transfers": [{"date": "2024-02-08", "from": "Hospital B", "to": "Hospital A",
                 "expected_patients": 0.7, "reason": "..."}],
  "notes": ["Recommendations are advisory. A bed manager decides."]
}
```

Suggested React screens: one card per hospital (a chart of p10 to p90 as a band, p50 as a line, `open_beds` as a second line, `beds` as a dashed limit), a 7-day reserves table, and a transfers table with the `reason` column. The bundled page `src/hcalloc/network/dashboard.html` is a working reference for all three.

## 4. Put it on GitHub

Claude could not reach your GitHub repository from its environment, so v2 is delivered as a zip. The zip's `hcalloc` folder is a git repository on a branch named `feature/v2-network-beds`, built on top of your GitHub `main` (after pull request #1). Sign in to GitHub as **vaibhav-c-mane**, then run one line at a time:

```
cd hcalloc
```
```
git push -u origin feature/v2-network-beds
```
If Git asks you to sign in, use the vaibhav-c-mane account. Then open https://github.com/vaibhav-c-mane/healthcare-resource-ai and click **Compare & pull request** for `feature/v2-network-beds`, then **Create pull request**, then **Merge**. The branch already contains the work from the open pull requests #2 (NHS data) and #3 (v1.1 results), so after merging you can close those two.

## 5. Code map (v2 parts)

| File | What it does |
| --- | --- |
| `src/hcalloc/network/model.py` | The optimiser: open beds now, transfers and unplaced patients per scenario; also scores a plan on what happened and computes perfect foresight under the same limits |
| `src/hcalloc/network/policies.py` | The method and six comparison policies |
| `src/hcalloc/network/simulate.py` | SYNTHETIC 3-hospital simulator with baseline, local, simultaneous and near-capacity surge regimes |
| `src/hcalloc/network/experiments.py` | E1 to E3 on the simulator and on two real NHS networks |
| `src/hcalloc/network/report.py` | Writes `results/network/REPORT.md` and figures |
| `src/hcalloc/network/service.py` | Forecast + plan + reasons for one day (used by the dashboard) |
| `src/hcalloc/network/api.py`, `dashboard.html` | FastAPI server and the bed dashboard page |
| `tests/test_network.py` | Tests for all of the above |

## 6. Next steps for the paper (next semester)

1. **Literature search first.** Find the published work on capacity pooling and patient-transfer networks, including work on synchronised versus out-of-step demand. Cite it, and position the paper as a measured answer to *when forecast-driven reserve coordination helps*, not as inventing pooling.
2. **Strengthen the evidence where reviewers will push:**
   - more NHS networks (the code takes any 3 neighbouring trusts: edit `NHS_NETWORKS` in `experiments.py`);
   - a sweep of how synchronised the surges are (between the local and simultaneous regimes);
   - the India case study: a simulator setting with fewer beds per person, hospitals running fuller, and a dengue-season surge.
3. **Be honest about limits.** Demand in the NHS data is beds occupied, which cannot exceed beds open, so true demand above capacity is hidden. A transfer counts as one bed-day; real transfers last several days. Costs and caps are assumed, which is why E3 sweeps them.
4. **Pick the venue.** Shortlist 2 or 3 conferences with IEEE, ACM or Springer proceedings, and confirm that this year's proceedings edition is Scopus-indexed before you submit.
5. **Write** in the conference template (usually 6 to 8 pages): problem, related work, method (sections 2 and 3 of `results/network/REPORT.md`), experiments, results, limits, conclusion.
