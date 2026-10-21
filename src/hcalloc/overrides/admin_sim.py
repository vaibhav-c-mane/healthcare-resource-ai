"""A simulated hospital administrator, used because no public data records real overrides.

SIMULATED BEHAVIOUR. The administrator plans with cost weights the system is
not told (by default more shortage-averse and more tail-averse than the
configured costs) and adds small habitual adjustments (noise). Results from
this simulator show whether the learner can recover hidden preferences; they
are not evidence about real administrators.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..allocate.model import solve_allocation
from ..config import HospitalConfig, PlanSettings


@dataclass
class SimulatedAdministrator:
    shortage_multiplier: float = 1.8  # deliberately off the learner's grid
    risk_weight: float = 0.5
    noise: float = 0.05  # probability that each unit-day bed or nurse figure is nudged by +-1
    seed: int = 0

    def preferred_plan(self, hospital: HospitalConfig, scenarios: np.ndarray) -> dict:
        settings = PlanSettings(risk_weight=self.risk_weight, shortage_multiplier=self.shortage_multiplier)
        return solve_allocation(hospital, scenarios, settings).first_stage()

    def finalise(self, hospital: HospitalConfig, preferred: dict, key: int) -> dict:
        """Apply habitual noise to the preferred plan, keeping it feasible."""
        rng = np.random.default_rng([self.seed, int(key)])
        final = {k: np.array(v, copy=True) for k, v in preferred.items()}
        for k, cap in (("beds", hospital.vector("beds")[:, None]), ("nurses", np.inf)):
            nudge = rng.choice([-1, 0, 1], size=final[k].shape, p=[self.noise / 2, 1 - self.noise, self.noise / 2])
            final[k] = np.clip(final[k] + nudge, 0, cap).astype(int)
        over = final["nurses"].sum(axis=0) > hospital.nurse_pool_max
        for t in np.flatnonzero(over):
            final["nurses"][np.argmax(final["nurses"][:, t]), t] -= final["nurses"][:, t].sum() - hospital.nurse_pool_max
        return final
