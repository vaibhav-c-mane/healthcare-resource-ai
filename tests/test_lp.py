import numpy as np

from hcalloc.allocate.lp import LinearProgram


def test_small_mip_matches_known_optimum():
    # max 3x + 2y  s.t. x + y <= 4, x + 3y <= 6, x <= 3, integers
    lp = LinearProgram()
    x = lp.add_vars((2,), lb=0, ub=[3, np.inf], cost=[-3, -2], integer=True)
    lp.add_constraints([(np.array([1.0, 1.0]), x[None, :], True)], ub=4)
    lp.add_constraints([(np.array([1.0, 3.0]), x[None, :], True)], ub=6)
    sol = lp.solve()
    assert np.allclose(sol.value(x), [3, 1])
    assert np.isclose(sol.objective, -11)


def test_broadcast_rows_and_sum_terms():
    lp = LinearProgram()
    v = lp.add_vars((2, 3), cost=1.0)
    rows = lp.add_constraints([(1.0, v, True)], lb=np.array([2.0, 5.0]))  # each row sums 3 variables
    assert rows.shape == (2,)
    sol = lp.solve()
    assert np.isclose(sol.objective, 7.0)
