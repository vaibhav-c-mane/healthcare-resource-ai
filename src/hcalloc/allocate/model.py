"""Two-stage stochastic allocation of fixed and movable hospital resources.

First stage (decided now, before demand is known), per unit u and day t:
    beds[u, t]    opened beds (fixed resource: cannot move between units)
    nurses[u, t]  rostered nurses
    pumps[u, t]   infusion pumps placed on the unit at the start of the day

Second stage (recourse, per demand scenario s, after demand is revealed):
    patients served on their home unit, outlying to an allowed host unit, or
    unserved (shortage); escalation (surge) beds, float-pool and agency nurses,
    nurse redeployments between compatible units, and pump transfers.

Coupling: a patient is served only if there is an open bed, enough nurse
capacity at the hard ratio of the patient's home unit, and enough pumps, all on
the unit where the patient is cared for. Nurse ratios are hard constraints;
unmet demand becomes shortage, which is costed and reported.

Objective: first-stage cost + (1 - lambda) * E[recourse] + lambda * CVaR_alpha[recourse].

Simplifications (documented in docs/assumptions.md): days are independent
given first-stage decisions, unserved patients do not carry over, outlying is
decided per day, and recourse variables are continuous.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import HospitalConfig, PlanSettings
from .lp import LinearProgram


@dataclass
class Plan:
    beds: np.ndarray
    nurses: np.ndarray
    pumps: np.ndarray
    first_stage_cost: float
    scenario_costs: np.ndarray
    objective: float
    settings: PlanSettings
    detail: dict = field(default_factory=dict)

    @property
    def expected_recourse(self) -> float:
        return float(self.scenario_costs.mean())

    def cvar(self, alpha: float = 0.9) -> float:
        return cvar(self.scenario_costs, alpha)

    def first_stage(self) -> dict:
        return {"beds": self.beds, "nurses": self.nurses, "pumps": self.pumps}


def cvar(costs: np.ndarray, alpha: float) -> float:
    """Mean of the worst (1 - alpha) share of costs (sample CVaR, Rockafellar-Uryasev form)."""
    costs = np.sort(np.asarray(costs, dtype=float))
    if costs.size == 0:
        return 0.0
    var = np.quantile(costs, alpha, method="inverted_cdf")
    return float(var + np.maximum(costs - var, 0).sum() / ((1 - alpha) * costs.size))


def first_stage_cost(hospital: HospitalConfig, beds: np.ndarray, nurses: np.ndarray) -> float:
    return float((hospital.vector("bed_cost")[:, None] * beds).sum() + hospital.nurse_cost * nurses.sum())


def solve_allocation(
    hospital: HospitalConfig,
    demand: np.ndarray,
    settings: PlanSettings = PlanSettings(),
    fixed: dict | None = None,
    ignore: frozenset[str] | set[str] = frozenset(),
    return_detail: bool = False,
) -> Plan:
    """Solve the allocation problem for demand scenarios of shape (S, U, T).

    ``fixed`` may hold any of ``beds``, ``nurses``, ``pumps`` (U, T) arrays to pin
    first-stage decisions (used to evaluate a plan and by the sequential baseline).
    ``ignore`` may contain ``"nurses"`` and/or ``"pumps"`` to drop that resource's
    constraints and costs (used by the sequential baseline, which plans one
    resource at a time).
    """
    demand = np.maximum(np.asarray(demand, dtype=float), 0.0)
    if demand.ndim == 2:
        demand = demand[None]
    s_count, n_units, horizon = demand.shape
    assert n_units == hospital.n_units
    fixed = fixed or {}

    beds_max = hospital.vector("beds")[:, None]
    surge_max = hospital.vector("surge_beds")[:, None, None]
    ratio = hospital.vector("patients_per_nurse")
    pump_need = hospital.vector("pumps_per_patient")
    agency_max = hospital.vector("agency_max")[:, None, None]
    float_ok = hospital.vector("float_ok")[:, None, None]
    shortage_cost = hospital.vector("shortage_cost") * settings.shortage_multiplier
    surge_cost = hospital.vector("surge_cost")
    bed_cost = hospital.vector("bed_cost")[:, None]
    use_nurses = "nurses" not in ignore
    use_pumps = "pumps" not in ignore
    shape2 = (n_units, horizon)
    shape3 = (n_units, horizon, s_count)

    lp = LinearProgram()

    def first_stage(name, ub, cost):
        if name in fixed:
            v = np.asarray(fixed[name], dtype=float)
            return lp.add_vars(shape2, lb=v, ub=v, cost=cost, integer=True)
        return lp.add_vars(shape2, lb=0, ub=ub, cost=cost, integer=True)

    beds = first_stage("beds", beds_max, bed_cost)
    if use_nurses:
        nurses = first_stage("nurses", hospital.nurse_pool_max, hospital.nurse_cost)
        lp.add_constraints([(1.0, nurses.T, True)], ub=hospital.nurse_pool_max)
    else:
        nurses = lp.add_vars(shape2, lb=0, ub=0)
    if use_pumps:
        pumps = first_stage("pumps", hospital.pump_fleet, 0.0)
        # every pump is placed on some unit each morning (no idle store)
        if "pumps" not in fixed:
            lp.add_constraints([(1.0, pumps.T, True)], lb=hospital.pump_fleet, ub=hospital.pump_fleet)
    else:
        pumps = lp.add_vars(shape2, lb=0, ub=0)

    served = lp.add_vars(shape3)
    short = lp.add_vars(shape3)
    surge = lp.add_vars(shape3, ub=surge_max)
    # outliers[home, host, t, s]: home-unit patients cared for in a host unit's bed
    outliers = lp.add_vars((n_units, n_units, horizon, s_count),
                           ub=np.where(hospital.overflow_matrix(), np.inf, 0.0)[:, :, None, None])
    out_of = outliers.transpose(0, 2, 3, 1)  # (home, t, s, host): patients sent away
    hosted = outliers.transpose(1, 2, 3, 0)  # (host, t, s, home): patients taken in
    d = demand.transpose(1, 2, 0)
    lp.add_constraints([(1.0, served), (1.0, short), (1.0, out_of, True)], lb=d, ub=d)
    lp.add_constraints([(1.0, served), (1.0, hosted, True), (-1.0, beds[:, :, None]), (-1.0, surge)], ub=0.0)

    cost_terms = [
        (-np.repeat(shortage_cost, horizon), short.transpose(2, 0, 1).reshape(s_count, -1), True),
        (-np.repeat(surge_cost, horizon), surge.transpose(2, 0, 1).reshape(s_count, -1), True),
        (-hospital.outlier_cost, outliers.transpose(3, 0, 1, 2).reshape(s_count, -1), True),
    ]

    detail_idx = {"served": served, "shortage": short, "surge": surge, "outliers": outliers}
    if use_nurses:
        allowed = hospital.move_matrix()
        floaters = lp.add_vars(shape3, ub=hospital.float_pool * float_ok)
        agency = lp.add_vars(shape3, ub=agency_max)
        moves = lp.add_vars((n_units, n_units, horizon, s_count), ub=np.where(allowed, np.inf, 0.0)[:, :, None, None])
        moves_out = moves.transpose(0, 2, 3, 1)  # (u, t, s, to)
        moves_in = moves.transpose(1, 2, 3, 0)  # (u, t, s, from)
        # nurse capacity at the hard ratio: served / ratio <= rostered - out + in + float + agency
        lp.add_constraints(
            [
                (1.0 / ratio[:, None, None], served),
                ((1.0 / ratio)[None, None, None, :], hosted, True),  # outliers keep their home unit's ratio
                (-1.0, nurses[:, :, None]),
                (1.0, moves_out, True),
                (-1.0, moves_in, True),
                (-1.0, floaters),
                (-1.0, agency),
            ],
            ub=0.0,
        )
        lp.add_constraints([(1.0, moves_out, True), (-1.0, nurses[:, :, None])], ub=0.0)
        lp.add_constraints([(1.0, floaters.transpose(1, 2, 0), True)], ub=hospital.float_pool)
        cost_terms += [
            (-hospital.agency_cost, agency.transpose(2, 0, 1).reshape(s_count, -1), True),
            (-hospital.nurse_move_cost, moves.transpose(3, 0, 1, 2).reshape(s_count, -1), True),
        ]
        detail_idx.update(floaters=floaters, agency=agency, nurse_moves=moves)

    if use_pumps:
        offdiag = ~np.eye(n_units, dtype=bool)
        pump_moves = lp.add_vars((n_units, n_units, horizon, s_count), ub=np.where(offdiag, np.inf, 0.0)[:, :, None, None])
        pumps_out = pump_moves.transpose(0, 2, 3, 1)
        pumps_in = pump_moves.transpose(1, 2, 3, 0)
        lp.add_constraints(
            [
                (pump_need[:, None, None], served),
                (pump_need[None, None, None, :], hosted, True),
                (-1.0, pumps[:, :, None]),
                (1.0, pumps_out, True),
                (-1.0, pumps_in, True),
            ],
            ub=0.0,
        )
        lp.add_constraints([(1.0, pumps_out, True), (-1.0, pumps[:, :, None])], ub=0.0)
        lp.add_constraints(
            [(1.0, pump_moves.transpose(2, 3, 0, 1).reshape(horizon, s_count, -1), True)],
            ub=hospital.pump_moves_max,
        )
        cost_terms.append((-hospital.pump_move_cost, pump_moves.transpose(3, 0, 1, 2).reshape(s_count, -1), True))
        detail_idx["pump_moves"] = pump_moves

    lam = float(settings.risk_weight)
    q = lp.add_vars((s_count,), lb=0.0, cost=(1.0 - lam) / s_count)
    lp.add_constraints([(1.0, q)] + cost_terms, lb=0.0, ub=0.0)
    if lam > 0:
        eta = lp.add_vars((1,), lb=-np.inf, cost=lam)
        xi = lp.add_vars((s_count,), lb=0.0, cost=lam / ((1.0 - settings.cvar_alpha) * s_count))
        lp.add_constraints([(1.0, xi), (-1.0, q), (1.0, eta)], lb=0.0)

    sol = lp.solve(time_limit=settings.time_limit, mip_gap=settings.mip_gap)
    b = np.rint(sol.value(beds)).astype(int)
    n = np.rint(sol.value(nurses)).astype(int)
    p = np.rint(sol.value(pumps)).astype(int)
    plan = Plan(
        beds=b,
        nurses=n,
        pumps=p,
        first_stage_cost=first_stage_cost(hospital, b, n),
        scenario_costs=sol.value(q),
        objective=sol.objective,
        settings=settings,
    )
    if return_detail:
        plan.detail = {k: sol.value(v) for k, v in detail_idx.items()}
    return plan
