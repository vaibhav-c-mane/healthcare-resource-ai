import numpy as np
import pytest

from hcalloc.allocate.model import cvar, solve_allocation
from hcalloc.config import PlanSettings
from hcalloc.evaluate.outcome import evaluate_plan, oracle


def demand(hospital, level, s=1, t=3):
    return np.broadcast_to(np.asarray(level, float)[None, :, None], (s, hospital.n_units, t)).copy()


def test_nurse_ratio_is_never_violated(hospital):
    d = demand(hospital, [30, 30, 100, 60])  # far above capacity
    plan = solve_allocation(hospital, d, return_detail=True)
    served = plan.detail["served"][..., 0]
    hosted = plan.detail["outliers"][..., 0]  # (home, host, T)
    ratio = hospital.vector("patients_per_nurse")
    moves = plan.detail["nurse_moves"][..., 0]
    cap = plan.nurses - moves.sum(axis=1) + moves.sum(axis=0) + plan.detail["floaters"][..., 0] + plan.detail["agency"][..., 0]
    need = served / ratio[:, None] + (hosted / ratio[:, None, None]).sum(axis=0)
    assert np.all(need <= cap + 1e-6)
    assert np.all(served + hosted.sum(axis=0) <= plan.beds + plan.detail["surge"][..., 0] + 1e-6)
    assert plan.detail["shortage"].sum() > 0


def test_overflow_moves_patients_to_spare_beds(hospital):
    from dataclasses import replace

    d = demand(hospital, [8, 10, 95, 20])  # Medical far over its 76 + 10 beds, Surgical nearly empty
    with_overflow = solve_allocation(hospital, d, return_detail=True)
    without = solve_allocation(replace(hospital, overflow=()), d, return_detail=True)
    out = with_overflow.detail["outliers"][..., 0]
    names = hospital.unit_names
    assert out[names.index("Medical"), names.index("Surgical")].sum() > 0
    assert out[names.index("Surgical"), names.index("Medical")].sum() < 1e-6
    assert with_overflow.detail["shortage"].sum() < without.detail["shortage"].sum()
    # outlying is never allowed into a lower level of care
    assert out[names.index("StepDown"), names.index("Medical")].sum() == 0


def test_low_demand_has_no_shortage(hospital):
    plan = solve_allocation(hospital, demand(hospital, [8, 10, 30, 20]), return_detail=True)
    assert plan.detail["shortage"].sum() < 1e-6
    assert np.all(plan.pumps.sum(axis=0) == hospital.pump_fleet)


def test_fixed_first_stage_is_respected(hospital):
    d = demand(hospital, [12, 15, 50, 35])
    fixed = {"beds": np.full((4, 3), 10), "nurses": np.full((4, 3), 5), "pumps": np.array([[60] * 3, [30] * 3, [30] * 3, [30] * 3])}
    plan = solve_allocation(hospital, d, fixed=fixed)
    assert np.all(plan.beds == 10) and np.all(plan.nurses == 5)


def test_oracle_is_a_lower_bound(hospital):
    rng = np.random.default_rng(0)
    real = rng.poisson([[15], [20], [60], [40]], size=(4, 7)).astype(float)
    scen = np.maximum(real[None] + rng.normal(0, 4, (20, 4, 7)), 0)
    plan = solve_allocation(hospital, scen, PlanSettings(risk_weight=0.3))
    assert evaluate_plan(hospital, plan.first_stage(), real).total_cost >= oracle(hospital, real).total_cost - 1e-6


def test_risk_weight_does_not_reduce_cvar_cost_of_plan(hospital):
    rng = np.random.default_rng(1)
    scen = np.maximum(rng.normal([[15], [20], [60], [40]], [[4], [4], [10], [8]], (30, 4, 7)), 0)
    neutral = solve_allocation(hospital, scen, PlanSettings(risk_weight=0.0, mip_gap=1e-4))
    averse = solve_allocation(hospital, scen, PlanSettings(risk_weight=0.9, mip_gap=1e-4))
    tail = lambda p: p.first_stage_cost + p.cvar(0.9)
    assert tail(averse) <= tail(neutral) + 1e-3 * tail(neutral)


def test_cvar_of_constant_is_constant():
    assert cvar(np.full(10, 5.0), 0.9) == pytest.approx(5.0)
    assert cvar(np.arange(10.0), 0.9) == pytest.approx(9.0)
