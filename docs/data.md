# Data

The planner reads only **daily aggregate counts per unit**:

```csv
date,unit,census,admissions,discharges
2023-01-01,ICU,15,4,3
2023-01-01,Medical,61,12,10
...
```

- `census`: patients on the unit at midnight at the end of the day
- `admissions`: arrivals to the unit during the day, including transfers in
- `discharges`: departures from the unit during the day, including transfers out

Every day must be present for every unit. Unit names must match the hospital configuration (`ICU`, `StepDown`, `Medical`, `Surgical` by default). At least about 15 months of history is needed: 12 months to calibrate the scenarios and 3 to fit.

## What was used for the results in this repository

- `results/` (top level): **semi-synthetic data.** The simulator in `hcalloc/data/synthetic.py` stands in for real demand; its parameters are in `assumptions.md`. Every output file and report from it is labelled synthetic, and `results/run_metadata.json` records the generator, seed and parameters.
- `results/nhs_sitrep/`: **real bed occupancy** from NHS England's UEC daily sitreps for three trusts over four winters (2021-22 to 2024-25), with staffing, pumps and costs assumed. See its `REPORT.md`; `run_metadata.json` lists the source files and every assumed value.

## Using real data

### MIMIC-IV (PhysioNet, credentialed)

1. Get access at <https://physionet.org/content/mimiciv/> (training plus a data use agreement). Keep the files on the machine the licence allows; do not upload them to online services.
2. Aggregate the `hosp/transfers` table to unit-level daily counts:

   ```python
   from hcalloc.data.loaders import aggregate_mimic_transfers
   series = aggregate_mimic_transfers("mimic-iv/hosp/transfers.csv.gz")
   series.to_csv("data/mimic_daily.csv")   # aggregate counts only
   ```

   Care units are mapped to the four planning units by `DEFAULT_CAREUNIT_MAP`; check the mapping against the care unit names in your extract.
3. MIMIC dates are shifted per patient into the future. Weekday and season are kept, but real holidays and calendar years are lost, and the date shift spreads one hospital's patients over a much longer synthetic calendar. Treat MIMIC-based census as a realistic length-of-stay and weekday structure, not as a real occupancy time series.
4. `aggregate_mimic_ed("mimic-iv-ed/ed/edstays.csv.gz")` gives daily ED arrivals and admissions, which can be added as a demand covariate.

### NHS England daily situation reports (open data)

The UEC daily sitrep "Web File Timeseries" workbooks publish, per trust and day for each winter (late November to March), adult general and acute (G&A) beds open and occupied and adult critical care beds open and occupied, as an 8am snapshot. They are aggregate, unvalidated management information under the Open Government Licence v3.0.

```bash
hcalloc sitrep-fetch                       # four winters into data/sitrep/ (about 10 MB)
hcalloc sitrep --trusts RA9 RNA RTD        # backtest + report into results/nhs_sitrep/ (about 2 minutes)
```

Or from Python:

```python
from hcalloc.data.loaders import clean_sitreps, load_uec_sitreps, sitrep_series
sitreps = clean_sitreps(load_uec_sitreps(["data/sitrep/uec_sitrep_2023-24.xlsx", ...]))
series = sitrep_series(sitreps, "RTD")     # units ICU (adult critical care) and G&A; census = beds occupied
```

- `load_uec_sitreps` reads the `Adult G&A beds` and `Adult critical care` sheets into one row per trust, unit and day. England totals computed from it match the published England line.
- `clean_sitreps` blanks obvious reporting errors (a day of zeros, a fourfold jump against the running median) so they count as missing; `sitrep_series` interpolates gaps of up to 3 days inside a winter.
- The series has gaps between winters. `DailySeries.observed` marks the days with data; forecasters fit and forecast only from origins with 28 observed days behind them and 7 ahead, and `season_phases` splits by winter (select on the third, test on the fourth) instead of `standard_phases`' calendar years.
- Sitreps do not publish admissions or discharges, so `default_forecasters(census_only=True)` leaves out the `flow` model and uses gradient boosting scaled by each origin's own 28-day level, because bed bases rise from one winter to the next.
- `trust_hospital` builds the assumed hospital (bed base = most beds opened in earlier winters; ratios, pools, pumps and costs from `StaffingAssumptions`).
- The tidy CSVs written to `results/nhs_sitrep/data/` have empty `admissions` and `discharges` and an extra `beds_open` column; read them with `DailySeries.read_csv(path, allow_gaps=True)`.

### HHS / CDC hospital capacity (US, open data)

Weekly facility-level beds and occupancy (HHS, 2020 to 2024) and weekly state-level respiratory hospital data (CDC NHSN, data.cdc.gov dataset `mpgq-jmmr`) are useful for regional trends and as external covariates. They are weekly, so they do not support daily planning directly, and no loader is included yet.

## Privacy

- Patient-level files are read only by the loader functions, on the machine that holds them, and only counts are returned.
- The planning service, app and decision log store aggregates, plan figures and free-text override notes. The app asks users not to type patient details in notes.
- Small counts can still be identifying when published. Before sharing aggregates outside the hospital, suppress small cells (for example below 5) or add noise.
