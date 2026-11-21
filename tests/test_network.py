"""Tests for the v2 network planner (beds across hospitals)."""

import numpy as np
import pytest

from hcalloc.network import POLICIES, NetworkConfig, evaluate_plan, make_policy_plan, oracle, plan_coordinated
from hcalloc.network.policies import independent_scenarios
from hcalloc.network.simulate import REGIMES, simulate_network


@pytest.fixture
def cfg():
    return NetworkConfig(("A", "B"), beds=(100, 100), transfer_cap=(5, 5))


def test_transfers_fill_spare_beds_up_to_the_cap(cfg):
    demand = np.array([[108.0], [80.0]])  # A is 8 over, B has 20 spare
    out = evaluate_plan(cfg, np.array([[100], [100]]), demand)
    assert out.transfers == pytest.approx(5)  # capped at 5 a day
    assert out.unmet_bed_days == pytest.approx(3)
    no_net = evaluate_plan(cfg.without_transfers(), np.array([[100], [100]]), demand)
    assert no_net.unmet_bed_days == pytest.approx(8) and no_net.transfers == 0


def test_oracle_obeys_physical_beds_and_cap(cfg):
    demand = np.array([[120.0], [99.0]])
    orc = oracle(cfg, demand)
    # A can open at most 100 beds and B has 1 spare bed: 19 patients cannot be placed even with perfect foresight
    assert orc.unmet_bed_days == pytest.approx(19)
    assert orc.transfers == pytest.approx(1)


def test_oracle_is_never_worse_than_any_policy(cfg):
    rng = np.random.default_rng(0)
    scen = np.array([90, 85])[None, :, None] + rng.normal(0, 6, (30, 2, 7))
    actual = np.array([[96, 99, 94, 101, 92, 90, 97], [80, 82, 88, 79, 85, 90, 84]], dtype=float)
    best = oracle(cfg, actual).cost
    for pol in POLICIES:
        plan, ecfg = make_policy_plan(pol, cfg, scen, np.median(scen, axis=0))
        assert plan.open.shape == (2, 7)
        assert (plan.open <= 100).all()
        assert evaluate_plan(ecfg, plan.open, actual).cost >= best - 1e-6


def test_coordinated_plan_beats_isolated_in_expectation(cfg):
    rng = np.random.default_rng(1)
    scen = np.array([95, 70])[None, :, None] + rng.normal(0, 8, (60, 2, 3))
    plan = plan_coordinated(cfg, scen)
    iso, iso_cfg = make_policy_plan("isolated", cfg, scen, np.median(scen, axis=0))
    coord_cost = np.mean([evaluate_plan(cfg, plan.open, s).cost for s in scen])
    iso_cost = np.mean([evaluate_plan(iso_cfg, iso.open, s).cost for s in scen])
    assert coord_cost <= iso_cost + 1e-6


def test_independent_scenarios_keep_marginals_but_break_correlation():
    rng = np.random.default_rng(2)
    common = rng.normal(0, 1, (400, 1, 1))
    scen = 50 + 10 * np.concatenate([common, common], axis=1)
    ind = independent_scenarios(scen)
    assert np.allclose(np.sort(ind[:, 0, 0]), np.sort(scen[:, 0, 0]))
    assert abs(np.corrcoef(ind[:, 0, 0], ind[:, 1, 0])[0, 1]) < 0.2


@pytest.mark.parametrize("regime", REGIMES)
def test_simulator_regimes(regime):
    s, surge = simulate_network(regime, seed=3)
    assert s.census.shape[1] == 3 and len(set(s.dates.year)) == 5
    test = s.dates.year == s.dates.year.max()
    if regime == "simultaneous":
        assert (surge[test].all(axis=1).sum()) > 30
    if regime == "local":
        assert surge[test].any(axis=1).sum() > 30 and surge[test].all(axis=1).sum() == 0
    if regime == "near_capacity":
        assert (s.census[test] / np.array([150, 250, 400])).mean() > 0.9


def test_history_is_shared_across_regimes():
    a, _ = simulate_network("local", seed=4)
    b, _ = simulate_network("simultaneous", seed=4)
    hist = a.dates.year < a.dates.year.max()
    assert np.array_equal(a.census[hist], b.census[hist])


def test_dashboard_api():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hcalloc.network.api import app

    c = TestClient(app)
    rng = c.get("/api/range").json()
    r = c.get(f"/api/plan?date={rng['default']}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["hospitals"]) == 3 and len(body["hospitals"][0]["days"]) == 7
    assert all(h["reasons"] for h in body["hospitals"])
    assert c.get("/api/plan?date=1990-01-01").status_code == 400
    assert "Bed planning" in c.get("/").text
