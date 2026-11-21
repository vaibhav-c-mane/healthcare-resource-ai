"""Bed-only planning across a network of hospitals: reserves, transfers and surge experiments.

This package is v2 of hcalloc. It plans one resource (open beds) for several
hospitals at once and lets patients move between hospitals at a fixed cost,
up to a fixed number per sending hospital per day.
"""

from .model import NetworkConfig, NetworkPlan, evaluate_plan, oracle, plan_coordinated
from .policies import POLICIES, make_policy_plan

__all__ = [
    "NetworkConfig",
    "NetworkPlan",
    "POLICIES",
    "evaluate_plan",
    "make_policy_plan",
    "oracle",
    "plan_coordinated",
]
