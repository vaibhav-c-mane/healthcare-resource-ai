"""Score first-stage decisions against the demand that actually happened."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from ..allocate.model import solve_allocation
from ..config import HospitalConfig, PlanSettings

TRUE_COST = PlanSettings(risk_weight=0.0, shortage_multiplier=1.0)


@dataclass
class Outcome:
    total_cost: float
    first_stage_cost: float
    recourse_cost: float
    shortage_cost: float
    shortage_patient_days: float
    shortage_unit_days: int  # unit-days with at least one unserved patient
    outlier_patient_days: float  # patients cared for outside their home unit
    surge_bed_days: float
    agency_nurse_days: float
    nurse_moves: float
    pump_moves: float
    idle_bed_days: float  # opened beds left empty
    excess_nurse_days: float  # rostered nurse-days above what served patients needed (hospital-wide, per day)
    unit_days: int

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_plan(hospital: HospitalConfig, first_stage: dict, realised: np.ndarray) -> Outcome:
    """Realised cost of a plan: fix first-stage decisions and solve the recourse for the real demand.

    Costs use the hospital's true cost parameters (no learned multipliers).
    """
    fixed = {k: np.asarray(first_stage[k]) for k in ("beds", "nurses", "pumps")}
    plan = solve_allocation(hospital, realised[None], TRUE_COST, fixed=fixed, return_detail=True)
    return outcome_from_plan(hospital, plan)


def oracle(hospital: HospitalConfig, realised: np.ndarray) -> Outcome:
    plan = solve_allocation(hospital, realised[None], TRUE_COST, return_detail=True)
    return outcome_from_plan(hospital, plan)


def outcome_from_plan(hospital: HospitalConfig, plan) -> Outcome:
    d = {k: v[..., 0] for k, v in plan.detail.items()}  # single scenario
    served, short, outl = d["served"], d["shortage"], d["outliers"]  # outliers: (home, host, T)
    ratio = hospital.vector("patients_per_nurse")[:, None]
    moves = d["nurse_moves"]
    needed = ((served + outl.sum(axis=1)) / ratio).sum(axis=0)  # every patient at its home unit's ratio
    occupied = served + outl.sum(axis=0)  # beds in use on each unit, own patients plus hosted ones
    shortage_cost = float((hospital.vector("shortage_cost")[:, None] * short).sum())
    return Outcome(
        total_cost=plan.first_stage_cost + float(plan.scenario_costs[0]),
        first_stage_cost=plan.first_stage_cost,
        recourse_cost=float(plan.scenario_costs[0]),
        shortage_cost=shortage_cost,
        shortage_patient_days=float(short.sum()),
        shortage_unit_days=int((short > 0.5).sum()),
        outlier_patient_days=float(outl.sum()),
        surge_bed_days=float(d["surge"].sum()),
        agency_nurse_days=float(d["agency"].sum()),
        nurse_moves=float(moves.sum()),
        pump_moves=float(d["pump_moves"].sum()),
        idle_bed_days=float(np.maximum(plan.beds - occupied, 0).sum()),
        excess_nurse_days=float(np.maximum(plan.nurses.sum(axis=0) - needed, 0).sum()),
        unit_days=int(served.size),
    )
