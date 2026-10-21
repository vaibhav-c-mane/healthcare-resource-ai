import numpy as np
import pytest

from hcalloc.forecast import ConformalScenarios, actuals, crps_ensemble, default_forecasters, interval_coverage


@pytest.mark.parametrize("f", default_forecasters(), ids=lambda f: f.name)
def test_shapes_and_no_leakage(series, f):
    end = series.index_of("2020-12-31")
    f.fit(series, end)
    origins = np.array([end + 10, end + 40])
    pred = f.predict_many(series, origins, 7)
    assert pred.shape == (2, len(series.units), 7)
    assert np.isfinite(pred).all() and (pred >= 0).all()
    # changing the future must not change forecasts made before it
    future = series.__class__(series.dates, series.units, series.census.copy(), series.admissions.copy(),
                              series.discharges.copy())
    for arr in (future.census, future.admissions, future.discharges):
        arr[end + 41:] += 50
    assert np.allclose(f.predict_many(future, origins, 7), pred)


def test_seasonal_naive_is_last_week(series):
    from hcalloc.forecast import SeasonalNaive

    o = 400
    pred = SeasonalNaive().predict(series, o, 7)
    assert np.allclose(pred, series.census[o - 6: o + 1].T)


def test_conformal_scenarios_cover_gaussian_noise():
    rng = np.random.default_rng(0)
    resid = rng.normal(0, 2, (500, 3, 7))
    gen = ConformalScenarios(200).calibrate(resid, np.zeros_like(resid))
    point = np.full((3, 7), 20.0)
    test_actual = point + rng.normal(0, 2, (300, 3, 7))
    scen = np.stack([gen.sample(point, key=k) for k in range(300)])
    assert abs(interval_coverage(scen, test_actual, 0.9) - 0.9) < 0.03


def test_crps_is_mae_for_single_member():
    a = np.array([[1.0, 2.0]])
    s = np.array([[[3.0, 2.0]]])
    assert crps_ensemble(s, a) == pytest.approx(1.0)


def test_actuals_alignment(series):
    a = actuals(series, [100], 3)
    assert np.allclose(a[0, :, 0], series.census[101])
