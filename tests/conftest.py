"""
conftest.py -- makes a plain `pytest` run honour these standalone test scripts.

The test files are deliberately pytest-free: each is a script that runs in its
own process against its own temp database and exits non-zero on failure (that
is how CI and update gating run them -- see .github/workflows/python-package.yml).
Their check() helper records failures in a module-level _FAILS list instead of
asserting, so without this shim a bare `pytest tests` would report green even
when checks failed. The autouse fixture below fails any test function whose
run grew its module's _FAILS list.

This file is only ever imported BY pytest; the scripts themselves never need it.
"""

import pytest


@pytest.fixture(autouse=True)
def _enforce_check_failures(request):
    fails = getattr(request.module, "_FAILS", None)
    before = len(fails) if fails is not None else 0
    yield
    if fails is not None and len(fails) > before:
        pytest.fail("check() failures: " + "; ".join(str(f) for f in fails[before:]),
                    pytrace=False)
