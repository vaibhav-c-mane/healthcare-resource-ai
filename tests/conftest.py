import pytest

from hcalloc.config import default_hospital
from hcalloc.data.synthetic import simulate_hospital


@pytest.fixture(scope="session")
def hospital():
    return default_hospital()


@pytest.fixture(scope="session")
def series():
    return simulate_hospital(seed=1, years=3)
