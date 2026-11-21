"""Two-stage stochastic bed planner for a network of hospitals.

Decided now (first stage), per hospital n and day h of the horizon:
    open[n, h]   beds opened and staffed, an integer between 0 and the hospital's physical beds.
Decided once demand is known (second stage, per scenario s):
    move[s, i, j, h]  patients sent from hospital i to hospital j on day h,
    unmet[s, n, h]    patients hospital n cannot place in an open bed (corridor care, cancellations).

Constraints, for every scenario, hospital and day:
    demand - moved out + moved in - unmet <= open        (everyone placed is in an open bed)
    moved out <= demand                                  (only patients who exist can move)
    moved out <= transfer_cap[n]                         (fixed daily cap per sending hospital)

Objective: open-bed cost + expected (transfer cost + unmet cost).

Days do not interact in this model: a transferred patient counts as one bed-day
at the receiving hospital on that day. That is the main simplification of v2.

The same model scores a plan after the fact (``evaluate_plan``: open fixed,
one scenario = what really happened) and gives the perfect-foresight benchmark
(``oracle``: open free, one scenario = what really happened). Both use the same
beds, transfer cap and costs as the plans they are compared with, so regret is
measured against a benchmark that obeys the same physical limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from ..allocate.lp import LinearProgram


@dataclass(frozen=True)
class NetworkConfig:
    names: tuple[str, ...]
    beds: tuple[int, ...]  # physical beds per hospital: the most that can ever be opened
    transfer_cap: tuple[int, ...]  # patients each hospital may send per day
    open_cost: float = 1.0  # cost of opening and staffing one bed for one day
    transfer_cost: float = 1.5  # cost of moving one patient (ambulance, handover, family distance)
    unmet_cost: float = 8.0  # cost of one patient-day without an open bed

    def __post_init__(self):
        if not (len(self.names) == len(self.beds) == len(self.transfer_cap)):
            raise ValueError("names, beds and transfer_cap must have one entry per hospital")

    @property
    def n(self) -> int:
        return len(self.names)

    def without_transfers(self) -> "NetworkConfig":
        return replace(self, transfer_cap=tuple(0 for _ in self.names))

    @classmethod
    def from_beds(cls, names, beds, cap_share: float = 0.02, **costs) -> "NetworkConfig":
        """Transfer cap = ``cap_share`` of each hospital's beds per day (at least 1 when cap_share > 0)."""
        caps = tuple(0 if cap_share <= 0 else max(1, int(round(cap_share * b))) for b in beds)
        return cls(tuple(names), tuple(int(b) for b in beds), caps, **costs)


@dataclass
class NetworkPlan:
    open: np.ndarray  # (N, H) beds to open
    expected_moves: np.ndarray | None = None  # (N, N, H) mean planned transfers over scenarios
    expected_unmet: np.ndarray | None = None  # (N, H)
    objective: float = float("nan")
    meta: dict = field(default_factory=dict)


def _build(cfg: NetworkConfig, demand: np.ndarray, open_fixed: np.ndarray | None):
    """demand: (S, N, H). Returns the program and the variable index blocks."""
    S, N, H = demand.shape
    if N != cfg.n:
        raise ValueError(f"demand has {N} hospitals, config has {cfg.n}")
    lp = LinearProgram()
    beds = np.asarray(cfg.beds, dtype=float)[:, None]
    if open_fixed is None:
        open_ = lp.add_vars((N, H), lb=0, ub=np.broadcast_to(beds, (N, H)), cost=cfg.open_cost, integer=True)
    else:
        open_ = lp.add_vars((N, H), lb=open_fixed, ub=open_fixed, cost=cfg.open_cost)
    w = 1.0 / S
    caps = np.asarray(cfg.transfer_cap, dtype=float)
    off_diag = ~np.eye(N, dtype=bool)
    ub_move = np.where(off_diag[None, :, :, None] & (caps[None, :, None, None] > 0), np.inf, 0.0)
    move = lp.add_vars((S, N, N, H), lb=0, ub=np.broadcast_to(ub_move, (S, N, N, H)), cost=w * cfg.transfer_cost)
    unmet = lp.add_vars((S, N, H), lb=0, cost=w * cfg.unmet_cost)

    out_idx = move  # (S, i, j, H): summing over j gives moved out of i
    in_idx = move.transpose(0, 2, 1, 3)  # (S, j, i, H): summing over i gives moved into j
    out_last = out_idx.transpose(0, 1, 3, 2)  # (S, N, H, N) so the sum runs over the last axis
    in_last = in_idx.transpose(0, 1, 3, 2)
    # demand - out + in - unmet - open <= 0
    lp.add_constraints(
        [(-1.0, out_last, True), (1.0, in_last, True), (-1.0, unmet), (-1.0, np.broadcast_to(open_, (S, N, H)))],
        ub=-demand,
    )
    lp.add_constraints([(1.0, out_last, True)], ub=demand)
    lp.add_constraints([(1.0, out_last, True)], ub=np.broadcast_to(caps[None, :, None], (S, N, H)))
    return lp, open_, move, unmet


def plan_coordinated(cfg: NetworkConfig, scenarios: np.ndarray, time_limit: float = 30.0) -> NetworkPlan:
    """Open beds for every hospital at once, anticipating transfers. scenarios: (S, N, H) bed demand."""
    scenarios = np.maximum(np.asarray(scenarios, dtype=float), 0.0)
    lp, open_, move, unmet = _build(cfg, scenarios, None)
    sol = lp.solve(time_limit=time_limit)
    o = np.round(sol.value(open_)).astype(int)
    return NetworkPlan(
        open=o,
        expected_moves=sol.value(move).mean(axis=0),
        expected_unmet=sol.value(unmet).mean(axis=0),
        objective=sol.objective,
    )


@dataclass
class Outcome:
    cost: float
    open_cost: float
    transfer_cost: float
    unmet_cost: float
    unmet_bed_days: float
    idle_bed_days: float
    transfers: float
    moves: np.ndarray  # (N, N, H)
    unmet: np.ndarray  # (N, H)
    idle: np.ndarray  # (N, H)


def _recourse(cfg: NetworkConfig, demand: np.ndarray, open_fixed: np.ndarray | None) -> tuple[np.ndarray, Outcome]:
    d = np.maximum(np.asarray(demand, dtype=float), 0.0)[None]
    lp, open_, move, unmet = _build(cfg, d, open_fixed)
    sol = lp.solve()
    o = sol.value(open_) if open_fixed is None else np.asarray(open_fixed, dtype=float)
    m = sol.value(move)[0]
    u = sol.value(unmet)[0]
    occupied = d[0] - m.sum(axis=1) + m.sum(axis=0) - u
    idle = np.maximum(o - occupied, 0.0)
    oc, tc, uc = cfg.open_cost * o.sum(), cfg.transfer_cost * m.sum(), cfg.unmet_cost * u.sum()
    return o, Outcome(
        cost=float(oc + tc + uc), open_cost=float(oc), transfer_cost=float(tc), unmet_cost=float(uc),
        unmet_bed_days=float(u.sum()), idle_bed_days=float(idle.sum()), transfers=float(m.sum()),
        moves=m, unmet=u, idle=idle,
    )


def evaluate_plan(cfg: NetworkConfig, open_beds: np.ndarray, actual: np.ndarray) -> Outcome:
    """Score a plan on what really happened. Transfers are chosen knowing that day's demand, within the cap."""
    open_beds = np.minimum(np.asarray(open_beds, dtype=float), np.asarray(cfg.beds, dtype=float)[:, None])
    return _recourse(cfg, actual, open_beds)[1]


def oracle(cfg: NetworkConfig, actual: np.ndarray) -> Outcome:
    """Perfect foresight under the same beds, transfer cap and costs."""
    return _recourse(cfg, actual, None)[1]
