"""Turn public or hospital data into the daily aggregate format (``DailySeries``).

Run these on the machine that holds the data. They read patient-level
extracts only to count, and return per-unit daily totals; nothing
patient-level is kept or written.

MIMIC-IV (PhysioNet, credentialed access) is the main public source with
unit-level stays. Its dates are shifted per patient, so calendar effects such
as holidays are not real; weekday and season are preserved.
"""

from __future__ import annotations

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
