import numpy as np

from hcalloc.allocate.model import solve_allocation
from hcalloc.overrides import OverrideLearner, SimulatedAdministrator, override_share, plan_distance


def test_learner_recovers_on_grid_preferences(hospital):
    rng = np.random.default_rng(0)
    learner = OverrideLearner(multipliers=(1.0, 3.0), risk_weights=(0.0, 0.6), prior=(1.0, 0.0))
    true = (3.0, 0.6)
    for k in range(4):
        scen = np.maximum(rng.normal([[15], [20], [62], [40]], [[5], [5], [12], [9]], (20, 4, 7)), 0)
        cands = {t: solve_allocation(hospital, scen, learner.settings(t)).first_stage() for t in learner.grid}
        learner.observe(cands, cands[true])
    assert learner.estimate() == true


def test_admin_noise_keeps_plan_feasible(hospital):
    admin = SimulatedAdministrator(noise=0.5)
    pref = {"beds": np.full((4, 7), 20), "nurses": np.full((4, 7), 12), "pumps": np.full((4, 7), 30)}
    final = admin.finalise(hospital, pref, key=1)
    assert (final["beds"] <= hospital.vector("beds")[:, None]).all()
    assert (final["nurses"].sum(axis=0) <= hospital.nurse_pool_max).all()
    assert plan_distance(pref, final) > 0
    assert 0 < override_share(pref, final) <= 1
