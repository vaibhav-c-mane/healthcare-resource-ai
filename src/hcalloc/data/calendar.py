"""Calendar features shared by the simulator and the forecasters."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
from dateutil.easter import easter


def bank_holidays(years) -> set[pd.Timestamp]:
    """UK-style public holidays. Swap in the local calendar when using a real hospital's data."""
    out: set[pd.Timestamp] = set()
    for y in years:
        e = pd.Timestamp(easter(y))
        mays = pd.date_range(f"{y}-05-01", f"{y}-05-31", freq="W-MON")
        aug = pd.date_range(f"{y}-08-01", f"{y}-08-31", freq="W-MON")
        out |= {
            pd.Timestamp(f"{y}-01-01"), e - pd.Timedelta(days=2), e + pd.Timedelta(days=1),
            mays[0], mays[-1], aug[-1],
            pd.Timestamp(f"{y}-12-24"), pd.Timestamp(f"{y}-12-25"), pd.Timestamp(f"{y}-12-26"), pd.Timestamp(f"{y}-12-31"),
        }
    return out


def holiday_flags(dates: pd.DatetimeIndex, horizon_pad: int = 31) -> np.ndarray:
    """1.0 on holidays for ``dates`` extended by ``horizon_pad`` days (so targets past the end are covered)."""
    return _holiday_flags(pd.Timestamp(dates[0]), pd.Timestamp(dates[-1]), horizon_pad)


@lru_cache(maxsize=32)
def _holiday_flags(first: pd.Timestamp, last: pd.Timestamp, horizon_pad: int) -> np.ndarray:
    ext = pd.date_range(first, last + pd.Timedelta(days=horizon_pad), freq="D")
    hol = bank_holidays(range(ext.year.min(), ext.year.max() + 1))
    flags = np.isin(ext, list(hol)).astype(float)
    flags.setflags(write=False)
    return flags
