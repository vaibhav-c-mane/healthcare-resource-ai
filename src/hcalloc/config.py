"""Hospital and planning configuration.

All costs are in relative cost units where 1.0 is the cost of one rostered
nurse-day. They are assumptions, not measurements: every experiment should be
re-run with the hospital's own values (see ``docs/assumptions.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np


@dataclass(frozen=True)
class Unit:
    """One inpatient unit (ward or ICU)."""

    name: str
    beds: int  # physical beds that can be opened (fixed resource)
    surge_beds: int  # escalation beds that can be opened at short notice
    patients_per_nurse: float  # hard staffing ratio
    pumps_per_patient: float  # average infusion pumps needed per patient
    shortage_cost: float  # per unserved patient-day (boarding, diversion, cancellation)
    bed_cost: float = 0.3  # per opened bed-day
    surge_cost: float = 1.5  # per escalation bed-day
    agency_max: int = 4  # agency / overtime nurses that can be called per day
    float_ok: bool = True  # whether float-pool nurses may work here


@dataclass(frozen=True)
class HospitalConfig:
    """Resources and costs for one hospital."""

    units: tuple[Unit, ...]
    # (from, to) pairs a rostered nurse may be redeployed along
    nurse_moves: tuple[tuple[str, str], ...]
    nurse_pool_max: int  # rostered nurses available per day across all units
    float_pool: int  # float-pool nurses per day (already paid, assigned on the day)
    pump_fleet: int  # mobile infusion pumps owned
    pump_moves_max: int = 20  # pump transfers logistics can do per day
    # (home, host) pairs: a home-unit patient may be cared for in a host unit's spare bed ("outlying")
    overflow: tuple[tuple[str, str], ...] = ()
    outlier_cost: float = 1.0  # per outlying patient-day (longer stays, extra handovers, safety risk)
    nurse_cost: float = 1.0
    agency_cost: float = 2.0
    nurse_move_cost: float = 0.15
    pump_move_cost: float = 0.1

    @property
    def unit_names(self) -> list[str]:
        return [u.name for u in self.units]

    @property
    def n_units(self) -> int:
        return len(self.units)

    def vector(self, attr: str) -> np.ndarray:
        return np.array([getattr(u, attr) for u in self.units], dtype=float)

    def move_matrix(self) -> np.ndarray:
        """Boolean (from, to) matrix of allowed nurse redeployments."""
        idx = {n: i for i, n in enumerate(self.unit_names)}
        m = np.zeros((self.n_units, self.n_units), dtype=bool)
        for a, b in self.nurse_moves:
            m[idx[a], idx[b]] = True
        np.fill_diagonal(m, False)
        return m

    def overflow_matrix(self) -> np.ndarray:
        """Boolean (home, host) matrix of allowed patient overflow."""
        idx = {n: i for i, n in enumerate(self.unit_names)}
        m = np.zeros((self.n_units, self.n_units), dtype=bool)
        for a, b in self.overflow:
            m[idx[a], idx[b]] = True
        np.fill_diagonal(m, False)
        return m

    def with_shortage_multiplier(self, k: float) -> "HospitalConfig":
        return replace(self, units=tuple(replace(u, shortage_cost=u.shortage_cost * k) for u in self.units))


def default_hospital() -> HospitalConfig:
    """A mid-sized hospital with four units, matched to the synthetic simulator."""
    units = (
        Unit("ICU", beds=20, surge_beds=4, patients_per_nurse=2, pumps_per_patient=3.0,
             shortage_cost=12.0, float_ok=False, agency_max=3),
        Unit("StepDown", beds=24, surge_beds=4, patients_per_nurse=3, pumps_per_patient=1.5,
             shortage_cost=6.0),
        Unit("Medical", beds=76, surge_beds=10, patients_per_nurse=5, pumps_per_patient=0.5,
             shortage_cost=6.0),
        Unit("Surgical", beds=48, surge_beds=6, patients_per_nurse=5, pumps_per_patient=0.8,
             shortage_cost=6.0),
    )
    # ICU nurses can float down to any unit; ward nurses move between wards only.
    moves = (
        ("ICU", "StepDown"), ("ICU", "Medical"), ("ICU", "Surgical"),
        ("StepDown", "Medical"), ("StepDown", "Surgical"),
        ("Medical", "Surgical"), ("Surgical", "Medical"),
    )
    # Patients may outlie only into a unit of the same or higher level of care.
    overflow = (
        ("Medical", "Surgical"), ("Surgical", "Medical"),
        ("Medical", "StepDown"), ("Surgical", "StepDown"),
        ("StepDown", "ICU"),
    )
    return HospitalConfig(units=units, nurse_moves=moves, nurse_pool_max=48, float_pool=4, pump_fleet=150,
                          overflow=overflow)


@dataclass(frozen=True)
class PlanSettings:
    """How the allocator trades expected cost against tail risk."""

    risk_weight: float = 0.3  # lambda: 0 = expected cost only, 1 = CVaR only
    cvar_alpha: float = 0.9  # CVaR over the worst (1 - alpha) share of scenarios
    shortage_multiplier: float = 1.0  # scales every unit's shortage cost (learned from overrides)
    time_limit: float = 60.0
    mip_gap: float = 5e-3  # 0.5%: about 4x faster than 0.1%, objective within 0.3% in tests

    def with_(self, **kw) -> "PlanSettings":
        return replace(self, **kw)


@dataclass(frozen=True)
class ExperimentConfig:
    horizon: int = 7
    n_scenarios: int = 40
    seed: int = 0
    scales: tuple[float, ...] = field(default=(0.75, 1.0, 1.25, 1.5))
