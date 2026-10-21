import numpy as np
import pandas as pd

from hcalloc.data.loaders import aggregate_mimic_transfers, aggregate_stays, map_careunit
from hcalloc.data.series import DailySeries
from hcalloc.data.synthetic import StressWave, simulate_hospital


def test_flow_identity(series):
    # census changes by admissions minus discharges
    d = series.census[1:] - series.census[:-1]
    assert np.allclose(d, series.admissions[1:] - series.discharges[1:])
    assert (series.census >= 0).all()


def test_stress_wave_changes_nothing_before_it_starts():
    a = simulate_hospital(seed=3, years=2)
    b = simulate_hospital(seed=3, years=2, stress=StressWave("2020-06-01"))
    i = a.index_of("2020-06-01")
    assert np.array_equal(a.census[:i], b.census[:i])
    assert b.census[i:i + 70].sum() != a.census[i:i + 70].sum()


def test_csv_round_trip(series, tmp_path):
    path = tmp_path / "s.csv"
    series.to_csv(path)
    back = DailySeries.read_csv(path, units=series.units)
    assert np.allclose(back.census, series.census)
    assert back.dates.equals(series.dates)


def test_aggregate_stays_counts_midnight_census():
    stays = pd.DataFrame({
        "unit": ["A", "A", "B"],
        "intime": ["2020-01-01 10:00", "2020-01-02 09:00", "2020-01-01 23:00"],
        "outtime": ["2020-01-03 08:00", "2020-01-02 18:00", "2020-01-02 01:00"],
    })
    s = aggregate_stays(stays, units=["A", "B"])
    # A: stay 1 present at midnight of Jan 1 and Jan 2; stay 2 leaves same day
    assert s.census[:, 0].tolist() == [1, 1, 0]
    assert s.census[:, 1].tolist() == [1, 0, 0]
    assert s.admissions[:, 0].tolist() == [1, 1, 0]
    assert s.discharges[:, 0].tolist() == [0, 1, 1]


def test_careunit_mapping():
    assert map_careunit("Medical Intensive Care Unit (MICU)") == "ICU"
    assert map_careunit("Med/Surg") == "Surgical"
    assert map_careunit("Medicine") == "Medical"
    assert map_careunit("Emergency Department") is None


def test_mimic_transfers_fixture():
    # tiny hand-made rows in the MIMIC-IV transfers schema (not real data)
    rows = pd.DataFrame({
        "eventtype": ["ED", "admit", "transfer", "discharge"],
        "careunit": ["Emergency Department", "Medicine", "Medical Intensive Care Unit (MICU)", None],
        "intime": ["2150-01-01 02:00", "2150-01-01 08:00", "2150-01-03 12:00", "2150-01-05 10:00"],
        "outtime": ["2150-01-01 08:00", "2150-01-03 12:00", "2150-01-05 10:00", None],
    })
    s = aggregate_mimic_transfers(rows, trim_days=0)
    assert s.units == ["ICU", "StepDown", "Medical", "Surgical"]
    assert s.census[:, 2].tolist()[:3] == [1, 1, 0]
    assert s.census[:, 0].tolist()[2:4] == [1, 1]
