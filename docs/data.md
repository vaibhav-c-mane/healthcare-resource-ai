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

**Semi-synthetic data only.** The build environment could not reach the public data hosts (NHS England, data.cdc.gov, healthdata.gov, PhysioNet, Zenodo were blocked by its network policy), and PhysioNet data needs credentialed access in any case. The simulator in `hcalloc/data/synthetic.py` stands in for real demand; its parameters are in `assumptions.md`. Every output file and report from it is labelled synthetic, and `results/run_metadata.json` records the generator, seed and parameters.

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

The UEC daily sitrep spreadsheets publish, per trust and day (mostly winter months), general and acute beds open and occupied, and critical care beds open and occupied. To use them:

1. Download the spreadsheets for the winters you need from the NHS England statistics site.
2. Pick one trust and build the tidy CSV with two units, for example `Medical` = G&A beds occupied and `ICU` = adult critical care beds occupied, with `census` from occupied beds.
3. Sitreps do not publish admissions or discharges. Use the `holt_winters`, `gbm`, `negbin_glm` or `seasonal_naive` forecasters, which need only census; the `flow` forecaster needs admissions and discharges.
4. Reduce the hospital configuration to those units (`HospitalConfig(units=...)`).

### HHS / CDC hospital capacity (US, open data)

Weekly facility-level beds and occupancy (HHS, 2020 to 2024) and weekly state-level respiratory hospital data (CDC NHSN) are useful for regional trends and as external covariates. They are weekly, so they do not support daily planning directly.

## Privacy

- Patient-level files are read only by the loader functions, on the machine that holds them, and only counts are returned.
- The planning service, app and decision log store aggregates, plan figures and free-text override notes. The app asks users not to type patient details in notes.
- Small counts can still be identifying when published. Before sharing aggregates outside the hospital, suppress small cells (for example below 5) or add noise.
