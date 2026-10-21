import datetime as dt

import numpy as np
import openpyxl
import pandas as pd
import pytest

from hcalloc.data.loaders import clean_sitreps, load_uec_sitreps, read_sitrep_sheet, sitrep_series
from hcalloc.data.series import DailySeries
from hcalloc.evaluate.backtest import season_phases
from hcalloc.forecast.models import default_forecasters, valid_origins
from hcalloc.sitrep import season_name, trust_hospital


def _write_workbook(path, start: str, days: int, trusts: dict) -> None:
    """A tiny workbook in the layout of the NHS 'Web File Timeseries' (hand-made numbers, not real data).

    ``trusts`` maps code -> (ga_open, ga_occ, cc_open, cc_occ) callables of the day number.
    """
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    dates = [dt.datetime.fromisoformat(start) + dt.timedelta(days=i) for i in range(days)]
    for sheet, labels, cols in (
        ("Adult G&A beds", ["Adult G&A Beds Open", "Adult G&A beds unavailable “void” ", "Adult G&A beds occ'd"], (0, None, 1)),
        ("Adult critical care", ["CC Adult Open", "CC Adult Occ"], (2, 3)),
    ):
        ws = wb.create_sheet(sheet)
        for _ in range(11):
            ws.append([None, "Title:", sheet])
        ws.append([None])
        ws.append([None, "Provider Level Data"])
        date_row, label_row = [None] * 5, [None, "NHS England Region", None, "Code", "Name"]
        for d in dates:
            date_row += [d] + [None] * (len(labels) - 1)
            label_row += labels
        ws.append(date_row)
        ws.append(label_row)
        ws.append([None, "-", None, "-", "ENGLAND (All Acute Trusts)"] + [999] * (len(labels) * days))
        ws.append([None])
        for code, f in trusts.items():
            row = [None, "Region", None, code, f"Trust {code}"]
            for i in range(days):
                row += [0 if c is None else f[c](i) for c in cols]
            ws.append(row)
    wb.save(path)


def _trust(ga=100.0, cc=10.0):
    return (lambda i: ga + 10, lambda i: ga + 5 * np.sin(i), lambda i: cc + 2, lambda i: cc + (i % 3))


@pytest.fixture()
def workbooks(tmp_path):
    paths = []
    for k, start in enumerate(("2021-11-29", "2022-11-28", "2023-11-27", "2024-11-25")):
        p = tmp_path / f"w{k}.xlsx"
        _write_workbook(p, start, 70, {"AAA": _trust(100 + 5 * k), "BBB": _trust(400, 30)})
        paths.append(p)
    return paths


def test_read_sitrep_sheet_layout(workbooks):
    df = read_sitrep_sheet(workbooks[0], "Adult G&A beds")
    assert set(df.trust_code) == {"AAA", "BBB"}  # England total row dropped
    assert set(df.measure) == {"beds_open", "beds_void", "beds_occupied"}
    first = df[(df.trust_code == "AAA") & (df.date == "2021-11-29")].set_index("measure").value
    assert first["beds_open"] == 110 and first["beds_occupied"] == pytest.approx(100.0)
    assert df.date.nunique() == 70


def test_load_and_series_with_gaps(workbooks):
    sitreps = load_uec_sitreps(workbooks)
    assert {"beds_open", "beds_occupied", "trust_name", "unit"} <= set(sitreps.columns)
    s = sitrep_series(sitreps, "AAA")
    assert s.units == ["ICU", "G&A"]
    assert len(s.segments()) == 4 and s.observed.sum() == 4 * 70
    assert not s.has_flows
    assert np.isnan(s.census[~s.observed]).all()
    a, b = s.segments()[1]
    assert s.dates[a] == pd.Timestamp("2022-11-28") and b - a + 1 == 70


def test_clean_blanks_errors_and_series_fills_short_gaps(workbooks):
    sitreps = load_uec_sitreps(workbooks)
    i = sitreps.index[(sitreps.trust_code == "BBB") & (sitreps.unit == "G&A") & (sitreps.date == "2022-12-10")][0]
    j = sitreps.index[(sitreps.trust_code == "BBB") & (sitreps.unit == "ICU") & (sitreps.date == "2022-12-20")][0]
    sitreps.loc[i, ["beds_open", "beds_occupied"]] = 0  # a non-submission recorded as zeros
    sitreps.loc[j, "beds_open"] = 320  # a typo: ten times the usual critical care beds
    clean = clean_sitreps(sitreps)
    assert clean.cleaned.sum() == 2
    assert clean.loc[clean.trust_code.eq("BBB") & clean.unit.eq("ICU"), "beds_open"].max() < 100
    s = sitrep_series(clean, "BBB")
    k = s.index_of("2022-12-10")
    assert s.observed[k] and 390 <= s.census[k, 1] <= 410  # interpolated from neighbours
    assert s.provenance["days_filled"] == 1


def test_gapped_csv_round_trip(workbooks, tmp_path):
    s = sitrep_series(load_uec_sitreps(workbooks), "AAA")
    path = tmp_path / "aaa.csv"
    s.to_csv(path)
    back = DailySeries.read_csv(path, units=s.units, allow_gaps=True)
    assert back.dates.equals(s.dates)
    assert np.array_equal(back.observed, s.observed)
    assert np.allclose(back.census[s.observed], s.census[s.observed])
    with pytest.raises(ValueError):
        DailySeries.read_csv(path, units=s.units)  # gaps are refused unless allowed


def test_season_phases_and_forecasters_stay_inside_seasons(workbooks):
    s = sitrep_series(load_uec_sitreps(workbooks), "AAA")
    ph = season_phases(s)
    runs = s.segments()
    sel, test = ph["select"], ph["test"]
    assert sel.fit_end == runs[0][1] and test.fit_end == runs[1][1]
    assert runs[1][0] + 27 <= sel.calib.min() and sel.calib.max() + 7 <= runs[1][1]
    assert all(runs[3][0] <= o <= runs[3][1] for o in test.evaluate)
    assert (s.dates[test.evaluate].dayofweek == 6).all()
    assert len(valid_origins(s, np.arange(len(s)), 7)) == 4 * (70 - 27 - 7)
    names = [f.name for f in default_forecasters(census_only=True)]
    assert "flow" not in names
    for f in default_forecasters(census_only=True):
        f.fit(s, test.fit_end)
        pred = f.predict_many(s, test.evaluate, 7)
        assert pred.shape == (len(test.evaluate), 2, 7) and np.isfinite(pred).all(), f.name


def test_trust_hospital_scales_with_beds(workbooks):
    sitreps = load_uec_sitreps(workbooks)
    small = trust_hospital(sitreps, "AAA", pd.Timestamp("2030-01-01"))
    big = trust_hospital(sitreps, "BBB", pd.Timestamp("2030-01-01"))
    assert [u.name for u in small.units] == ["ICU", "G&A"]
    assert small.units[1].beds == 125  # most G&A beds open: 100 + 5 * 3 + 10
    assert big.nurse_pool_max > small.nurse_pool_max and big.pump_fleet > small.pump_fleet
    assert small.overflow == () and small.nurse_moves == (("ICU", "G&A"),)


def test_season_name():
    assert season_name("2024-12-01") == "2024-25" and season_name("2025-03-01") == "2024-25"
