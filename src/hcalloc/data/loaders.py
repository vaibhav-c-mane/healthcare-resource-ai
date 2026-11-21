"""Turn public or hospital data into the daily aggregate format (``DailySeries``).

Run these on the machine that holds the data. They read patient-level
extracts only to count, and return per-unit daily totals; nothing
patient-level is kept or written.

MIMIC-IV (PhysioNet, credentialed access) is the main public source with
unit-level stays. Its dates are shifted per patient, so calendar effects such
as holidays are not real; weekday and season are preserved.

NHS England UEC daily situation reports (open data, Open Government Licence)
give real per-trust bed counts for each winter; they are already aggregate.
"""

from __future__ import annotations

import datetime as dt
import re

import numpy as np
import pandas as pd

from .series import DailySeries

# MIMIC-IV hosp.transfers careunit names -> planning units. Order matters: first match wins.
DEFAULT_CAREUNIT_MAP: tuple[tuple[str, str], ...] = (
    (r"intensive care|\bicu\b|\(micu|\(sicu|\(cvicu|\(ccu|\(tsicu|neuro surgical intensive", "ICU"),
    (r"intermediate|step.?down|\bimcu\b", "StepDown"),
    (r"surg|ortho|transplant|thoracic|vascular|plastic", "Surgical"),
    (r"medic|cardiology|oncology|neurology|hematology|observation", "Medical"),
)


def map_careunit(name: str, mapping=DEFAULT_CAREUNIT_MAP) -> str | None:
    if not isinstance(name, str):
        return None
    low = name.lower()
    for pattern, unit in mapping:
        if re.search(pattern, low):
            return unit
    return None


def aggregate_stays(
    stays: pd.DataFrame,
    unit_col: str = "unit",
    start_col: str = "intime",
    end_col: str = "outtime",
    units: list[str] | None = None,
) -> DailySeries:
    """Daily census (at midnight), admissions and discharges per unit from stay segments.

    ``stays`` has one row per continuous stay in a unit with start and end timestamps.
    """
    df = stays[[unit_col, start_col, end_col]].dropna().copy()
    df[start_col] = pd.to_datetime(df[start_col])
    df[end_col] = pd.to_datetime(df[end_col])
    df = df[df[end_col] > df[start_col]]
    units = units or sorted(df[unit_col].unique())
    first = df[start_col].min().normalize()
    last = df[end_col].max().normalize()
    dates = pd.date_range(first, last, freq="D")
    n = len(dates)
    census = np.zeros((n, len(units)))
    adm = np.zeros_like(census)
    dis = np.zeros_like(census)
    for j, u in enumerate(units):
        g = df[df[unit_col] == u]
        s_day = ((g[start_col].dt.normalize() - first).dt.days).to_numpy()
        e_day = ((g[end_col].dt.normalize() - first).dt.days).to_numpy()
        adm[:, j] = np.bincount(s_day, minlength=n)[:n]
        dis[:, j] = np.bincount(e_day, minlength=n)[:n]
        # present at midnight ending day d  <=>  start_day <= d < end_day
        delta = np.bincount(s_day, minlength=n + 1)[: n + 1].astype(float)
        delta -= np.bincount(e_day, minlength=n + 1)[: n + 1]
        census[:, j] = np.cumsum(delta)[:n]
    return DailySeries(dates, list(units), census, adm, dis, provenance={"kind": "aggregated stays"})


def aggregate_mimic_transfers(path_or_frame, mapping=DEFAULT_CAREUNIT_MAP, units=("ICU", "StepDown", "Medical", "Surgical"),
                              trim_days: int = 30) -> DailySeries:
    """MIMIC-IV ``hosp/transfers.csv.gz`` -> per-unit daily series.

    Uses rows with eventtype 'admit' or 'transfer' (stays on a ward); ED and
    discharge rows are ignored. ``trim_days`` drops the ragged start and end of
    the extract, where stays are cut off.
    """
    df = path_or_frame if isinstance(path_or_frame, pd.DataFrame) else pd.read_csv(
        path_or_frame, usecols=["eventtype", "careunit", "intime", "outtime"]
    )
    df = df[df["eventtype"].isin(["admit", "transfer"])].copy()
    df["unit"] = df["careunit"].map(lambda c: map_careunit(c, mapping))
    df = df[df["unit"].isin(units)]
    series = aggregate_stays(df, units=list(units))
    if trim_days:
        keep = slice(trim_days, len(series) - trim_days)
        series = DailySeries(series.dates[keep], series.units, series.census[keep], series.admissions[keep],
                             series.discharges[keep])
    series.provenance = {"kind": "real (MIMIC-IV, date-shifted)", "source": "hosp/transfers", "careunit_map": mapping}
    return series


def aggregate_mimic_ed(path_or_frame) -> pd.DataFrame:
    """MIMIC-IV-ED ``edstays.csv.gz`` -> daily ED arrivals and admissions (a demand covariate)."""
    df = path_or_frame if isinstance(path_or_frame, pd.DataFrame) else pd.read_csv(
        path_or_frame, usecols=["intime", "disposition"]
    )
    df["date"] = pd.to_datetime(df["intime"]).dt.normalize()
    out = df.groupby("date").agg(ed_arrivals=("intime", "size"),
                                 ed_admitted=("disposition", lambda s: (s == "ADMITTED").sum()))
    return out.asfreq("D", fill_value=0)


# NHS England UEC daily sitrep "Web File Timeseries" workbooks ------------------------------------

# planning unit -> sheet in the timeseries workbook
SITREP_SHEETS: dict[str, str] = {"G&A": "Adult G&A beds", "ICU": "Adult critical care"}


def _sitrep_measure(label) -> str | None:
    low = str(label).lower()
    if "void" in low or "unavailable" in low:
        return "beds_void"
    if "occ" in low:
        return "beds_occupied"
    if "open" in low:
        return "beds_open"
    return None


def read_sitrep_sheet(path, sheet: str) -> pd.DataFrame:
    """One sheet of a UEC daily sitrep timeseries workbook -> long frame.

    The sheet has a row of dates (each date heads a group of measure columns),
    a row of measure labels under it, then one row per trust: region, code, name,
    values. Returns columns date, region, trust_code, trust_name, measure, value
    for trust rows only (England and regional totals are dropped).
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(wb[sheet].iter_rows(values_only=True))
    finally:
        wb.close()
    date_row = next(i for i, r in enumerate(rows) if sum(isinstance(c, dt.datetime) for c in r) >= 2)
    labels = rows[date_row + 1]
    code_col = next(j for j, c in enumerate(labels) if str(c).strip().lower() == "code")
    columns, current = [], None
    for j, c in enumerate(rows[date_row]):
        if isinstance(c, dt.datetime):
            current = pd.Timestamp(c).normalize()
        measure = _sitrep_measure(labels[j]) if j > code_col + 1 else None
        if current is not None and measure:
            columns.append((j, current, measure))
    records = []
    for r in rows[date_row + 2:]:
        code = r[code_col] if len(r) > code_col else None
        if not isinstance(code, str) or not code.strip() or code.strip() == "-":
            continue
        for j, date, measure in columns:
            records.append((date, r[1], code.strip(), r[code_col + 1], measure, r[j]))
    df = pd.DataFrame(records, columns=["date", "region", "trust_code", "trust_name", "measure", "value"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def load_uec_sitreps(paths, sheets: dict[str, str] = SITREP_SHEETS) -> pd.DataFrame:
    """Several winters of sitrep workbooks -> one row per trust, unit and day.

    Columns: date, region, trust_code, trust_name, unit, beds_open, beds_occupied
    (plus beds_void where published). Bed counts are an 8am snapshot.
    """
    parts = []
    for path in paths:
        for unit, sheet in sheets.items():
            long = read_sitrep_sheet(path, sheet).assign(unit=unit, source=str(path))
            parts.append(long)
    long = pd.concat(parts, ignore_index=True)
    # a day can appear in two workbooks when winters overlap; keep the later file's value
    long = long.drop_duplicates(["date", "trust_code", "unit", "measure"], keep="last")
    wide = long.pivot_table(index=["date", "trust_code", "unit"], columns="measure", values="value", aggfunc="first")
    names = long.drop_duplicates("trust_code", keep="last").set_index("trust_code")[["region", "trust_name"]]
    out = wide.reset_index().join(names, on="trust_code")
    out.columns.name = None
    cols = ["date", "region", "trust_code", "trust_name", "unit", "beds_open", "beds_occupied"]
    cols += [c for c in out.columns if c not in cols]
    return out[cols].sort_values(["trust_code", "unit", "date"], ignore_index=True)


def clean_sitreps(sitreps: pd.DataFrame, window: int = 15, low: float = 0.5, high: float = 2.0,
                  min_level: float = 20.0) -> pd.DataFrame:
    """Blank out bed counts that are clearly reporting errors, so they are treated as missing.

    A value is dropped when it is zero while the trust usually reports beds, or, where the
    running median over ``window`` published days is at least ``min_level``, when it is
    below ``low`` or above ``high`` times that median (for example 387 critical care beds
    on one day at a trust that has about 85). Small units are left alone, because their
    day-to-day swings are real. Adds a ``cleaned`` column flagging changed rows.
    """
    out = sitreps.sort_values(["trust_code", "unit", "date"]).copy()
    out["cleaned"] = False
    for col in ("beds_open", "beds_occupied"):
        med = out.groupby(["trust_code", "unit"])[col].transform(
            lambda x: x.rolling(window, center=True, min_periods=5).median())
        x = out[col]
        bad = ((x == 0) & (med > 0)) | ((med >= min_level) & ((x < low * med) | (x > high * med)))
        out.loc[bad, col] = np.nan
        out["cleaned"] |= bad
    return out.reset_index(drop=True)


def sitrep_series(sitreps: pd.DataFrame, trust_code: str, units=("ICU", "G&A"), max_fill_days: int = 3) -> DailySeries:
    """One trust's occupied beds as a census-only ``DailySeries`` with gaps between winters.

    ``census`` is occupied beds at the 8am snapshot. Admissions and discharges are
    not published, so they are NaN. Missing days inside a winter of up to
    ``max_fill_days`` are filled by linear interpolation; longer holes stay unobserved.
    """
    df = sitreps[(sitreps.trust_code == trust_code) & sitreps.unit.isin(units)]
    if df.empty:
        raise ValueError(f"no sitrep rows for trust {trust_code!r}")
    wide = df.pivot_table(index="date", columns="unit", values="beds_occupied", aggfunc="first").reindex(columns=list(units))
    full = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
    published = full.isin(df.date.unique())
    wide = wide.reindex(full)
    before = int(wide.isna().any(axis=1)[published].sum())
    filled = wide.interpolate(limit=max_fill_days, limit_area="inside")
    filled[~published] = np.nan  # never fill across the months between winters
    census = filled.to_numpy(dtype=float)
    observed = ~np.isnan(census).any(axis=1)
    nan = np.full_like(census, np.nan)
    return DailySeries(
        full, list(units), np.where(observed[:, None], census, np.nan), nan, nan.copy(),
        provenance={
            "kind": "real (NHS England UEC daily sitrep)",
            "trust_code": trust_code,
            "trust_name": str(df.trust_name.iloc[-1]),
            "census": "beds occupied at 8am snapshot",
            "units": {u: SITREP_SHEETS.get(u, u) for u in units},
            "days_published": int(published.sum()),
            "days_filled": before - int((~observed & published).sum()),
            "days_dropped": int((~observed & published).sum()),
            "values_cleaned": int(df["cleaned"].sum()) if "cleaned" in df else 0,
            "licence": "Open Government Licence v3.0",
        },
        observed=observed,
    )
