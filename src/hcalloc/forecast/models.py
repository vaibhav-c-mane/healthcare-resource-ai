"""Census forecasters. Every model sees only aggregate daily series up to the forecast origin.

``predict_many(series, origins, horizon)`` returns point forecasts of shape
(K, U, H): census on days origin+1 .. origin+H for each unit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.optimize import minimize
from sklearn.ensemble import HistGradientBoostingRegressor

from ..data.calendar import holiday_flags
from ..data.series import DailySeries


def target_index(origins, horizon: int) -> np.ndarray:
    return np.asarray(origins)[:, None] + np.arange(1, horizon + 1)[None, :]


def actuals(series: DailySeries, origins, horizon: int) -> np.ndarray:
    """Realised census, shape (K, U, H)."""
    return series.census[target_index(origins, horizon)].transpose(0, 2, 1)


def _calendar(series: DailySeries, idx: np.ndarray) -> dict:
    dates = series.dates[0] + pd.to_timedelta(idx.ravel(), unit="D")
    doy = dates.dayofyear.to_numpy().reshape(idx.shape)
    return {
        "dow": dates.dayofweek.to_numpy().reshape(idx.shape),
        "sin": np.sin(2 * np.pi * doy / 365.25),
        "cos": np.cos(2 * np.pi * doy / 365.25),
        "holiday": holiday_flags(series.dates)[idx],
    }


def _same_weekday_lag(idx: np.ndarray, origin: np.ndarray) -> np.ndarray:
    """Most recent observed day with the same weekday as each target."""
    steps = np.ceil((idx - origin[:, None]) / 7).astype(int)
    return idx - 7 * steps


class Forecaster:
    name = "base"

    def fit(self, series: DailySeries, end: int) -> "Forecaster":
        return self

    def predict_many(self, series: DailySeries, origins, horizon: int) -> np.ndarray:
        raise NotImplementedError

    def predict(self, series: DailySeries, origin: int, horizon: int) -> np.ndarray:
        return self.predict_many(series, [origin], horizon)[0]


class SeasonalNaive(Forecaster):
    """Census on the same weekday last week."""

    name = "seasonal_naive"

    def predict_many(self, series, origins, horizon):
        origins = np.asarray(origins)
        idx = _same_weekday_lag(target_index(origins, horizon), origins)
        return series.census[idx].transpose(0, 2, 1)


class HoltWinters(Forecaster):
    """Additive Holt-Winters (ETS A,N,A) with a weekly season, one model per unit.

    Smoothing parameters are fitted once on the training window; at each origin
    the states are re-filtered on the last ``window`` days, which is cheap.
    """

    name = "holt_winters"

    def __init__(self, window: int = 365):
        self.window = window
        self.params: list[tuple[float, float]] = []

    @staticmethod
    def _filter(y: np.ndarray, alpha: float, gamma: float):
        level = y[:7].mean()
        season = y[:7] - level
        sse = 0.0
        for t in range(7, len(y)):
            s = season[t % 7]
            err = y[t] - (level + s)
            sse += err * err
            new_level = alpha * (y[t] - s) + (1 - alpha) * level
            season[t % 7] = gamma * (y[t] - new_level) + (1 - gamma) * s
            level = new_level
        return level, season, sse

    def fit(self, series, end):
        self.params = []
        for j in range(len(series.units)):
            y = series.census[max(0, end - 729): end + 1, j]
            res = minimize(
                lambda p: self._filter(y, *p)[2], x0=[0.3, 0.1], bounds=[(0.01, 0.99), (0.01, 0.99)], method="L-BFGS-B"
            )
            self.params.append(tuple(res.x))
        return self

    def predict_many(self, series, origins, horizon):
        origins = np.asarray(origins)
        out = np.zeros((len(origins), len(series.units), horizon))
        for k, o in enumerate(origins):
            start = max(0, o - self.window + 1)
            for j, (alpha, gamma) in enumerate(self.params):
                y = series.census[start: o + 1, j]
                level, season, _ = self._filter(y.copy(), alpha, gamma)
                n = len(y)
                out[k, j] = level + season[(n + np.arange(horizon)) % 7]
        return np.maximum(out, 0.0)


class GradientBoosting(Forecaster):
    """Global direct multi-horizon gradient-boosted trees (scikit-learn HistGradientBoosting).

    One model for all units and horizons on scale-free features. ``quantile``
    sets the training loss: 0.5 gives the median; a higher value is the
    cost-weighted variant (newsvendor critical fractile).
    """

    name = "gbm"

    def __init__(self, quantile: float = 0.5, max_iter: int = 300, seed: int = 0):
        self.quantile = quantile
        self.max_iter = max_iter
        self.seed = seed
        if quantile != 0.5:
            self.name = f"gbm_q{int(round(quantile * 100))}"

    def _design(self, series, origins, horizon):
        origins = np.asarray(origins)
        c = series.census / self.scale_c
        a = series.admissions / self.scale_a
        n_units = c.shape[1]
        cs = np.vstack([np.zeros((1, n_units)), np.cumsum(c, axis=0)])
        tgt = target_index(origins, horizon)
        cal = _calendar(series, tgt)
        lag_c = np.stack([c[origins - k] for k in range(14)], axis=-1)  # (K, U, 14)
        lag_a = np.stack([a[origins - k] for k in range(7)], axis=-1)  # (K, U, 7)
        mean7 = (cs[origins + 1] - cs[origins - 6]) / 7
        mean28 = (cs[origins + 1] - cs[origins - 27]) / 28
        last_week = c[_same_weekday_lag(tgt, origins)].transpose(0, 2, 1)  # (K, U, H)
        shape = (len(origins), n_units, horizon)

        def b(x):
            return np.broadcast_to(x, shape)[..., None]

        cols = [
            b(np.arange(n_units)[None, :, None]),
            b(cal["dow"][:, None, :]),
            b(np.arange(1, horizon + 1)[None, None, :]),
            b(cal["sin"][:, None, :]),
            b(cal["cos"][:, None, :]),
            b(cal["holiday"][:, None, :]),
            b(last_week),
            b(mean7[:, :, None]),
            b(mean28[:, :, None]),
            np.broadcast_to(lag_c[:, :, None, :], shape + (14,)),
            np.broadcast_to(lag_a[:, :, None, :], shape + (7,)),
        ]
        return np.concatenate(cols, axis=-1).reshape(-1, sum(x.shape[-1] for x in cols))

    def fit(self, series, end):
        self.scale_c = series.census[: end + 1].mean(axis=0)
        self.scale_a = np.maximum(series.admissions[: end + 1].mean(axis=0), 1e-6)
        horizon = 7
        origins = np.arange(27, end - horizon + 1)
        x = self._design(series, origins, horizon)
        y = (actuals(series, origins, horizon) / self.scale_c[None, :, None]).ravel()
        self.model = HistGradientBoostingRegressor(
            loss="quantile",
            quantile=self.quantile,
            max_iter=self.max_iter,
            learning_rate=0.05,
            min_samples_leaf=40,
            categorical_features=[0, 1],
            random_state=self.seed,
        ).fit(x, y)
        return self

    def predict_many(self, series, origins, horizon):
        x = self._design(series, origins, horizon)
        pred = self.model.predict(x).reshape(len(origins), len(series.units), horizon)
        return np.maximum(pred * self.scale_c[None, :, None], 0.0)


class NegBinGLM(Forecaster):
    """Negative binomial regression per unit on lagged census and calendar terms."""

    name = "negbin_glm"

    def _design(self, series, origins, horizon, j):
        origins = np.asarray(origins)
        c = series.census[:, j]
        tgt = target_index(origins, horizon)
        cal = _calendar(series, tgt)
        cs = np.concatenate([[0.0], np.cumsum(c)])
        mean7 = (cs[origins + 1] - cs[origins - 6]) / 7
        k, h = tgt.shape
        cols = [
            np.ones((k, h)),
            np.broadcast_to(np.log1p(c[origins])[:, None], (k, h)),
            np.log1p(c[_same_weekday_lag(tgt, origins)]),
            np.broadcast_to(np.log1p(mean7)[:, None], (k, h)),
            cal["sin"],
            cal["cos"],
            cal["holiday"],
        ]
        cols += [(cal["dow"] == d).astype(float) for d in range(1, 7)]
        cols += [np.broadcast_to((np.arange(1, horizon + 1) == s).astype(float)[None, :], (k, h)) for s in range(2, horizon + 1)]
        return np.stack(cols, axis=-1).reshape(k * h, -1)

    def fit(self, series, end):
        horizon = 7
        origins = np.arange(27, end - horizon + 1)
        self.models = []
        for j in range(len(series.units)):
            x = self._design(series, origins, horizon, j)
            y = series.census[target_index(origins, horizon), j].ravel()
            pois = sm.GLM(y, x, family=sm.families.Poisson()).fit()
            mu = pois.mu
            alpha = max(1e-4, float(np.mean(((y - mu) ** 2 - mu) / mu**2)))
            self.models.append(sm.GLM(y, x, family=sm.families.NegativeBinomial(alpha=alpha)).fit())
        return self

    def predict_many(self, series, origins, horizon):
        out = np.zeros((len(origins), len(series.units), horizon))
        for j, model in enumerate(self.models):
            out[:, j, :] = model.predict(self._design(series, origins, horizon, j)).reshape(len(origins), horizon)
        return out


class FlowModel(Forecaster):
    """Stock-and-flow model built from aggregates only.

    census[t] = census[t-1] * (1 - p_discharge[unit, weekday]) + admissions[t]
    with admissions = recent 28-day level x weekday factor. It can also sample
    scenarios natively (binomial discharges, negative binomial admissions).
    """

    name = "flow"

    def fit(self, series, end):
        lo = max(1, end - 729)
        dow = series.dates.dayofweek.to_numpy()
        adm = series.admissions[lo: end + 1]
        self.adm_factor = np.stack([adm[dow[lo: end + 1] == d].mean(axis=0) for d in range(7)]) / adm.mean(axis=0)
        dis = series.discharges[lo: end + 1]
        prev = series.census[lo - 1: end]
        self.p_dis = np.stack(
            [dis[dow[lo: end + 1] == d].sum(axis=0) / prev[dow[lo: end + 1] == d].sum(axis=0) for d in range(7)]
        )
        # Level uncertainty: spread of next-week mean admissions around the trailing 28-day mean.
        cs = np.vstack([np.zeros((1, adm.shape[1])), np.cumsum(adm, axis=0)])
        t = np.arange(28, len(adm) - 7)
        trailing = (cs[t] - cs[t - 28]) / 28
        ahead = (cs[t + 7] - cs[t]) / 7
        self.level_cv = np.std(np.log(np.maximum(ahead, 1e-3) / np.maximum(trailing, 1e-3)), axis=0)
        return self

    def _setup(self, series, origin, horizon):
        lvl = series.admissions[origin - 27: origin + 1].mean(axis=0)
        tgt = np.arange(origin + 1, origin + horizon + 1)
        dow = _calendar(series, tgt[None])["dow"][0]
        return lvl, dow

    def predict_many(self, series, origins, horizon):
        out = np.zeros((len(origins), len(series.units), horizon))
        for k, o in enumerate(origins):
            lvl, dow = self._setup(series, o, horizon)
            c = series.census[o].copy()
            for h in range(horizon):
                c = c * (1 - self.p_dis[dow[h]]) + lvl * self.adm_factor[dow[h]]
                out[k, :, h] = c
        return out

    def sample(self, series, origin, horizon, n, rng) -> np.ndarray:
        lvl, dow = self._setup(series, origin, horizon)
        c = np.broadcast_to(series.census[origin], (n, len(series.units))).astype(int)
        level = lvl[None] * rng.gamma(1 / self.level_cv**2, self.level_cv**2, (n, len(series.units)))
        out = np.zeros((n, len(series.units), horizon))
        for h in range(horizon):
            c = c - rng.binomial(c, self.p_dis[dow[h]][None])
            c = c + rng.poisson(level * self.adm_factor[dow[h]][None])
            out[:, :, h] = c
        return out


def default_forecasters() -> list[Forecaster]:
    return [SeasonalNaive(), HoltWinters(), GradientBoosting(), NegBinGLM(), FlowModel()]
