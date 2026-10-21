"""Planning methods compared in the benchmarks. Each returns first-stage decisions.

rule           occupancy-target rule on a point forecast (common practice)
deterministic  optimiser on the point forecast (no uncertainty)
quantile       optimiser on a high quantile of the scenarios (a safety buffer)
sequential     stochastic, but beds, then nurses, then pumps planned one after another
joint          stochastic, all resources together, expected cost
joint_cvar     stochastic, all resources together, expected cost + CVaR
oracle         optimiser with the realised demand (perfect foresight lower bound)
"""

from __future__ import annotations

import numpy as np

from ..config import HospitalConfig, PlanSettings
from .model import Plan, solve_allocation


def _proportional_integer(weights: np.ndarray, total: int) -> np.ndarray:
    """Split ``total`` integer units across rows of ``weights`` (U, T), per column."""
    w = np.maximum(weights, 0)
    share = np.where(w.sum(axis=0) > 0, w / np.maximum(w.sum(axis=0), 1e-9), 1 / w.shape[0]) * total
    out = np.floor(share).astype(int)
    for t in range(w.shape[1]):
        rem = total - out[:, t].sum()
        if rem > 0:
            out[np.argsort(-(share[:, t] - out[:, t]))[:rem], t] += 1
    return out


def rule_plan(hospital: HospitalConfig, point: np.ndarray, target_occupancy: float = 0.85) -> dict:
    """Open beds so forecast occupancy is at the target, staff those beds at ratio, share pumps by need."""
    beds = np.minimum(np.ceil(point / target_occupancy), hospital.vector("beds")[:, None]).astype(int)
    nurses = np.ceil(beds / hospital.vector("patients_per_nurse")[:, None]).astype(int)
    over = nurses.sum(axis=0) > hospital.nurse_pool_max
    if over.any():
        nurses[:, over] = _proportional_integer(nurses[:, over].astype(float), hospital.nurse_pool_max)
    need = beds * hospital.vector("pumps_per_patient")[:, None]
    pumps = _proportional_integer(need, hospital.pump_fleet)
    return {"beds": beds, "nurses": nurses, "pumps": pumps}


def sequential_plan(hospital: HospitalConfig, scenarios: np.ndarray, settings: PlanSettings) -> Plan:
    """Plan beds alone, then nurses for those beds, then pumps for those beds and nurses."""
    beds = solve_allocation(hospital, scenarios, settings, ignore={"nurses", "pumps"}).beds
    nurses = solve_allocation(hospital, scenarios, settings, fixed={"beds": beds}, ignore={"pumps"}).nurses
    return solve_allocation(hospital, scenarios, settings, fixed={"beds": beds, "nurses": nurses})


METHODS = ("rule", "deterministic", "quantile", "sequential", "joint", "joint_cvar")


def make_plan(
    method: str,
    hospital: HospitalConfig,
    point: np.ndarray,
    scenarios: np.ndarray,
    settings: PlanSettings,
    quantile: float = 0.8,
) -> dict:
    """First-stage decisions (beds, nurses, pumps) for one planning origin."""
    if method == "rule":
        return rule_plan(hospital, point)
    if method == "deterministic":
        return solve_allocation(hospital, point[None], settings.with_(risk_weight=0.0)).first_stage()
    if method == "quantile":
        q = np.quantile(scenarios, quantile, axis=0)
        return solve_allocation(hospital, q[None], settings.with_(risk_weight=0.0)).first_stage()
    if method == "sequential":
        return sequential_plan(hospital, scenarios, settings).first_stage()
    if method == "joint":
        return solve_allocation(hospital, scenarios, settings.with_(risk_weight=0.0)).first_stage()
    if method == "joint_cvar":
        return solve_allocation(hospital, scenarios, settings).first_stage()
    raise ValueError(f"unknown method {method!r}")
