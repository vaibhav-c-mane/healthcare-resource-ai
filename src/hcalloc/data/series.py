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
    """

    dates: pd.DatetimeIndex
    units: list[str]
    census: np.ndarray
    admissions: np.ndarray
    discharges: np.ndarray
    provenance: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.dates = pd.DatetimeIndex(self.dates)
        for name in METRICS:
            arr = np.asarray(getattr(self, name), dtype=float)
            if arr.shape != (len(self.dates), len(self.units)):
                raise ValueError(f"{name} has shape {arr.shape}, expected {(len(self.dates), len(self.units))}")
            setattr(self, name, arr)

    def __len__(self) -> int:
        return len(self.dates)

    def index_of(self, date) -> int:
        return int(self.dates.get_loc(pd.Timestamp(date)))

    def year_indices(self, year: int) -> np.ndarray:
        return np.flatnonzero(self.dates.year == year)

    def to_frame(self) -> pd.DataFrame:
        """Tidy frame: date, unit, census, admissions, discharges."""
        parts = []
        for j, unit in enumerate(self.units):
            parts.append(
                pd.DataFrame(
                    {
                        "date": self.dates,
                        "unit": unit,
                        "census": self.census[:, j],
                        "admissions": self.admissions[:, j],
                        "discharges": self.discharges[:, j],
                    }
                )
            )
        return pd.concat(parts, ignore_index=True)

    @classmethod
    def from_frame(cls, df: pd.DataFrame, units: list[str] | None = None, provenance: dict | None = None) -> "DailySeries":
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        units = units or sorted(df["unit"].unique())
        wide = df.pivot_table(index="date", columns="unit", values=list(METRICS), aggfunc="sum")
        full = pd.date_range(wide.index.min(), wide.index.max(), freq="D")
        wide = wide.reindex(full)
        if wide.isna().any().any():
            missing = int(wide.isna().any(axis=1).sum())
            raise ValueError(f"{missing} days have missing values; fill or trim them before use")
        return cls(
            dates=full,
            units=list(units),
            census=wide["census"][units].to_numpy(),
            admissions=wide["admissions"][units].to_numpy(),
            discharges=wide["discharges"][units].to_numpy(),
            provenance=provenance or {},
        )

    def to_csv(self, path) -> None:
        self.to_frame().to_csv(path, index=False)

    @classmethod
    def read_csv(cls, path, units: list[str] | None = None) -> "DailySeries":
        return cls.from_frame(pd.read_csv(path), units=units, provenance={"source": str(path)})
