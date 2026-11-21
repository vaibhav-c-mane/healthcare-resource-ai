"""Aggregate daily hospital series: the only data the forecasting and allocation layers see."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

METRICS = ("census", "admissions", "discharges")


@dataclass
class DailySeries:
    """Daily per-unit counts. Rows are days, columns are units.

    census[t, u]      patients in unit u at midnight ending day t
    admissions[t, u]  patients admitted to unit u during day t
    discharges[t, u]  patients leaving unit u during day t

    ``observed[t]`` is False on days with no data (for example the months
    between winters in NHS sitreps). Their values are NaN, and forecasters and
    backtests use only origins whose history and targets are observed.
    Admissions and discharges are NaN throughout for census-only sources.
    """

    dates: pd.DatetimeIndex
    units: list[str]
    census: np.ndarray
    admissions: np.ndarray
    discharges: np.ndarray
    provenance: dict = field(default_factory=dict)
    observed: np.ndarray | None = None

    def __post_init__(self) -> None:
        self.dates = pd.DatetimeIndex(self.dates)
        for name in METRICS:
            arr = np.asarray(getattr(self, name), dtype=float)
            if arr.shape != (len(self.dates), len(self.units)):
                raise ValueError(f"{name} has shape {arr.shape}, expected {(len(self.dates), len(self.units))}")
            setattr(self, name, arr)
        if self.observed is None:
            self.observed = np.ones(len(self.dates), dtype=bool)
        self.observed = np.asarray(self.observed, dtype=bool)
        if self.observed.shape != (len(self.dates),):
            raise ValueError("observed must have one flag per day")
        if np.isnan(self.census[self.observed]).any():
            raise ValueError("census is missing on days marked observed")

    @property
    def has_flows(self) -> bool:
        """True when admissions and discharges are known on every observed day."""
        obs = self.observed
        return bool(np.isfinite(self.admissions[obs]).all() and np.isfinite(self.discharges[obs]).all())

    def segments(self) -> list[tuple[int, int]]:
        """Runs of consecutive observed days as (first, last) index pairs, inclusive."""
        flags = np.concatenate([[False], self.observed, [False]]).astype(int)
        starts = np.flatnonzero(np.diff(flags) == 1)
        ends = np.flatnonzero(np.diff(flags) == -1) - 1
        return list(zip(starts.tolist(), ends.tolist()))

    def segment_start(self, i: int) -> int:
        """First index of the observed run that contains day ``i``."""
        for a, b in self.segments():
            if a <= i <= b:
                return a
        raise ValueError(f"day {i} is not observed")

    def __len__(self) -> int:
        return len(self.dates)

    def index_of(self, date) -> int:
        return int(self.dates.get_loc(pd.Timestamp(date)))

    def year_indices(self, year: int) -> np.ndarray:
        return np.flatnonzero(self.dates.year == year)

    def to_frame(self) -> pd.DataFrame:
        """Tidy frame: date, unit, census, admissions, discharges (observed days only)."""
        parts = []
        obs = self.observed
        for j, unit in enumerate(self.units):
            parts.append(
                pd.DataFrame(
                    {
                        "date": self.dates[obs],
                        "unit": unit,
                        "census": self.census[obs, j],
                        "admissions": self.admissions[obs, j],
                        "discharges": self.discharges[obs, j],
                    }
                )
            )
        return pd.concat(parts, ignore_index=True)

    @classmethod
    def from_frame(cls, df: pd.DataFrame, units: list[str] | None = None, provenance: dict | None = None,
                   allow_gaps: bool = False) -> "DailySeries":
        """Build from a tidy frame.

        With ``allow_gaps``, days absent from the frame become unobserved, and
        ``admissions``/``discharges`` may be missing or empty (census-only data).
        Otherwise every day from first to last must have every value.
        """
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        for name in METRICS:
            if name not in df:
                if not allow_gaps:
                    raise ValueError(f"column {name!r} is missing")
                df[name] = np.nan
        units = units or sorted(df["unit"].unique())
        wide = df.pivot_table(index="date", columns="unit", values=list(METRICS), aggfunc="sum", min_count=1,
                              dropna=False)
        full = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
        wide = wide.reindex(full)
        census = wide["census"].reindex(columns=units).to_numpy(dtype=float, copy=True)
        flows = [wide[m].reindex(columns=units).to_numpy(dtype=float) for m in ("admissions", "discharges")]
        if allow_gaps:
            observed = ~np.isnan(census).any(axis=1)
            census[~observed] = np.nan
        else:
            observed = None
            missing = int((np.isnan(census) | np.isnan(flows[0]) | np.isnan(flows[1])).any(axis=1).sum())
            if missing:
                raise ValueError(f"{missing} days have missing values; fill or trim them before use")
        return cls(
            dates=full,
            units=list(units),
            census=census,
            admissions=flows[0],
            discharges=flows[1],
            provenance=provenance or {},
            observed=observed,
        )

    def to_csv(self, path) -> None:
        self.to_frame().to_csv(path, index=False)

    @classmethod
    def read_csv(cls, path, units: list[str] | None = None, allow_gaps: bool = False) -> "DailySeries":
        return cls.from_frame(pd.read_csv(path), units=units, provenance={"source": str(path)}, allow_gaps=allow_gaps)
