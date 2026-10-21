"""A small vectorised builder for mixed-integer linear programs, solved with HiGHS via SciPy.

Variables are created in numpy-shaped blocks and constraints are written as
lists of ``(coef, idx)`` terms that broadcast to one row per element. A term
``(coef, idx, True)`` sums over the last axis of ``idx`` within each row.
This keeps model building fast enough for thousands of repeated solves
without a modelling-language dependency.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, milp


class SolveError(RuntimeError):
    pass


@dataclass
class LPSolution:
    x: np.ndarray
    objective: float
    status: int
    message: str

    def value(self, idx: np.ndarray) -> np.ndarray:
        return self.x[idx]


class LinearProgram:
    def __init__(self) -> None:
        self.num_vars = 0
        self.num_rows = 0
        self._lb: list[np.ndarray] = []
        self._ub: list[np.ndarray] = []
        self._cost: list[np.ndarray] = []
        self._int: list[np.ndarray] = []
        self._rows: list[np.ndarray] = []
        self._cols: list[np.ndarray] = []
        self._vals: list[np.ndarray] = []
        self._rlb: list[np.ndarray] = []
        self._rub: list[np.ndarray] = []

    def add_vars(self, shape, lb=0.0, ub=np.inf, cost=0.0, integer: bool = False) -> np.ndarray:
        shape = tuple(np.atleast_1d(shape))
        n = int(np.prod(shape))
        idx = np.arange(self.num_vars, self.num_vars + n).reshape(shape)
        self.num_vars += n
        self._lb.append(np.broadcast_to(np.asarray(lb, dtype=float), shape).ravel().copy())
        self._ub.append(np.broadcast_to(np.asarray(ub, dtype=float), shape).ravel().copy())
        self._cost.append(np.broadcast_to(np.asarray(cost, dtype=float), shape).ravel().copy())
        self._int.append(np.full(n, 1 if integer else 0, dtype=np.uint8))
        return idx

    def add_constraints(self, terms, lb=-np.inf, ub=np.inf) -> np.ndarray:
        parsed = []
        shapes = []
        for term in terms:
            coef, idx = np.asarray(term[0], dtype=float), np.asarray(term[1])
            summed = len(term) > 2 and bool(term[2])
            parsed.append((coef, idx, summed))
            shapes.append(idx.shape[:-1] if summed else idx.shape)
        shape = np.broadcast_shapes(*shapes, np.shape(lb), np.shape(ub))
        nrows = int(np.prod(shape))
        row_ids = self.num_rows + np.arange(nrows).reshape(shape)
        for coef, idx, summed in parsed:
            if summed:
                full = shape + (idx.shape[-1],)
                rows = np.broadcast_to(row_ids[..., None], full)
            else:
                full = shape
                rows = row_ids
            self._rows.append(rows.ravel())
            self._cols.append(np.broadcast_to(idx, full).ravel())
            self._vals.append(np.broadcast_to(coef, full).ravel())
        self._rlb.append(np.broadcast_to(np.asarray(lb, dtype=float), shape).ravel().copy())
        self._rub.append(np.broadcast_to(np.asarray(ub, dtype=float), shape).ravel().copy())
        self.num_rows += nrows
        return row_ids

    def solve(self, time_limit: float = 60.0, mip_gap: float = 1e-3) -> LPSolution:
        c = np.concatenate(self._cost)
        integrality = np.concatenate(self._int)
        bounds = Bounds(np.concatenate(self._lb), np.concatenate(self._ub))
        constraints = []
        if self.num_rows:
            a = sparse.csr_matrix(
                (np.concatenate(self._vals), (np.concatenate(self._rows), np.concatenate(self._cols))),
                shape=(self.num_rows, self.num_vars),
            )
            constraints = [LinearConstraint(a, np.concatenate(self._rlb), np.concatenate(self._rub))]
        options = {"time_limit": time_limit, "disp": False}
        if integrality.any():
            options["mip_rel_gap"] = mip_gap
        res = milp(c, constraints=constraints, integrality=integrality, bounds=bounds, options=options)
        if res.x is None:
            raise SolveError(f"HiGHS returned no solution: {res.message}")
        return LPSolution(x=res.x, objective=float(res.fun), status=int(res.status), message=str(res.message))
