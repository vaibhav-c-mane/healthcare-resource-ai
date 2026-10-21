import numpy as np

from hcalloc.config import PlanSettings
from hcalloc.service import DecisionLog, plan_next_week, recalibrate


def test_plan_log_and_recalibrate(series, hospital, tmp_path):
    res = plan_next_week(series, forecaster="seasonal_naive", n_scenarios=15, settings=PlanSettings(risk_weight=0.0))
    table = res.table()
    assert len(table) == 4 * 7 and {"beds", "nurses", "pumps"} <= set(table.columns)
    log = DecisionLog(tmp_path / "d.sqlite")
    final = {k: v.copy() for k, v in res.plan.first_stage().items()}
    final["beds"] = final["beds"] + 1
    log.record(res, final, reason="risk appetite: want more buffer")
    log.record(res, final, reason="risk appetite: want more buffer")
    rows = log.load()
    assert len(rows) == 2 and np.array_equal(rows[0]["final"]["beds"], final["beds"])
    assert rows[0]["scenarios"].shape == res.scenarios.shape
    theta, learner = recalibrate(log, hospital, PlanSettings(risk_weight=0.0))
    assert theta in learner.grid
