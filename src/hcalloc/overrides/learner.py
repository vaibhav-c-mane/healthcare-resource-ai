"""Learning from administrator overrides.

Each override is a pair (recommended plan, final plan). The learner treats the
final plan as the decision of an optimiser whose cost weights it cannot see,
and estimates those weights by decision matching: among candidate settings
(shortage-cost multiplier, risk weight) it keeps the one whose plans are
closest to what administrators actually chose. This is inverse optimisation
over a small, bounded parameter grid.

Guardrails: only cost weights are learned. Hard constraints (nurse ratios,
capacity) are never relaxed, the grid bounds how far weights can move, and
every change is logged for human review before it is applied.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from ..config import PlanSettings

Theta = tuple[float, float]  # (shortage multiplier, risk weight)


def plan_distance(a: dict, b: dict, keys=("beds", "nurses")) -> float:
    """L1 distance between two first-stage plans over the given resources."""
    return float(sum(np.abs(np.asarray(a[k]) - np.asarray(b[k])).sum() for k in keys))


def override_share(recommended: dict, final: dict, keys=("beds", "nurses")) -> float:
    """Share of unit-day decisions the administrator changed."""
    changed = sum((np.asarray(recommended[k]) != np.asarray(final[k])).sum() for k in keys)
    total = sum(np.asarray(recommended[k]).size for k in keys)
    return float(changed / total)


@dataclass
class OverrideLearner:
    multipliers: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5, 3.0)
    risk_weights: tuple[float, ...] = (0.0, 0.3, 0.6)
    prior: Theta = (1.0, 0.3)
    min_observations: int = 2
    decay: float = 0.97  # older overrides count a little less
    scores: dict = field(default_factory=dict)
    n_obs: int = 0
    log: list = field(default_factory=list)

    @property
    def grid(self) -> list[Theta]:
        return list(itertools.product(self.multipliers, self.risk_weights))

    def settings(self, theta: Theta, base: PlanSettings = PlanSettings()) -> PlanSettings:
        return base.with_(shortage_multiplier=theta[0], risk_weight=theta[1])

    def observe(self, candidates: dict[Theta, dict], final: dict) -> None:
        """``candidates`` maps each grid setting to the plan it would have produced for this week."""
        for theta in self.grid:
            d = plan_distance(candidates[theta], final)
            self.scores[theta] = self.decay * self.scores.get(theta, 0.0) + d
        self.n_obs += 1

    def estimate(self) -> Theta:
        if self.n_obs < self.min_observations or not self.scores:
            theta = self.prior
        else:
            # ties broken toward the prior, so the learner moves only when evidence favours it
            theta = min(self.grid, key=lambda t: (round(self.scores[t], 6), abs(t[0] - self.prior[0]) + abs(t[1] - self.prior[1])))
        self.log.append({"n_obs": self.n_obs, "multiplier": theta[0], "risk_weight": theta[1]})
        return theta
