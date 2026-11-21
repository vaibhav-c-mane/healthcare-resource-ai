"""Planning policies compared in the experiments. Each returns open beds (N, H) and the network it is scored on.

coordinated        stochastic plan for all hospitals at once, joint scenarios, transfers anticipated (the method)
coordinated_indep  same planner, but each hospital's scenario is drawn independently: hospitals never spike
                   together in the plan's view of the future (ablation: removes cross-hospital correlation)
point_coordinated  same planner on the point forecast only (ablation: removes uncertainty)
alone_reactive     each hospital plans alone (newsvendor on its own scenarios); transfers happen only on the day
isolated           each hospital plans alone and never transfers (no network at all)
rule85             open enough beds for the median forecast to be 85% full; transfers on the day
p80                open beds = 80th percentile of the forecast; transfers on the day
"""

from __future__ import annotations

import numpy as np

from .model import NetworkConfig, NetworkPlan, plan_coordinated

POLICIES = ("coordinated", "coordinated_indep", "point_coordinated", "alone_reactive", "isolated", "rule85", "p80")
METHOD = "coordinated"


def critical_ratio(cfg: NetworkConfig) -> float:
    """Newsvendor quantile for one hospital alone: open a bed while P(it is needed) x unmet cost > open cost."""
    return max(0.0, min(1.0, (cfg.unmet_cost - cfg.open_cost) / cfg.unmet_cost))


def _cap(cfg: NetworkConfig, x: np.ndarray) -> np.ndarray:
    beds = np.asarray(cfg.beds)[:, None]
    return np.clip(np.ceil(x - 1e-9), 0, beds).astype(int)


def independent_scenarios(scenarios: np.ndarray, seed: int = 0) -> np.ndarray:
    """Shuffle scenarios separately for each hospital, keeping each hospital's own spread but no co-movement."""
    rng = np.random.default_rng(seed)
    out = scenarios.copy()
    for n in range(scenarios.shape[1]):
        out[:, n] = scenarios[rng.permutation(len(scenarios)), n]
    return out


def make_policy_plan(policy: str, cfg: NetworkConfig, scenarios: np.ndarray, point: np.ndarray,
                     seed: int = 0) -> tuple[NetworkPlan, NetworkConfig]:
    """scenarios: (S, N, H) joint demand scenarios; point: (N, H) median forecast."""
    if policy == "coordinated":
        return plan_coordinated(cfg, scenarios), cfg
    if policy == "coordinated_indep":
        return plan_coordinated(cfg, independent_scenarios(scenarios, seed)), cfg
    if policy == "point_coordinated":
        return plan_coordinated(cfg, point[None]), cfg
    if policy in ("alone_reactive", "isolated"):
        o = _cap(cfg, np.quantile(scenarios, critical_ratio(cfg), axis=0))
        return NetworkPlan(open=o), (cfg if policy == "alone_reactive" else cfg.without_transfers())
    if policy == "rule85":
        return NetworkPlan(open=_cap(cfg, point / 0.85)), cfg
    if policy == "p80":
        return NetworkPlan(open=_cap(cfg, np.quantile(scenarios, 0.8, axis=0))), cfg
    raise ValueError(f"unknown policy {policy!r}; choose from {POLICIES}")
