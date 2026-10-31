"""One marker for tests that drive a real optional SDK call (#117).

From aitp-sdk 0.24, TCT renewal, session bundles and multi-hop delegation
are built only with the ``experimental`` feature. A test that reaches one of
them carries ``@requires_sdk_feature("<key>")``:

- on a wheel without the feature the test is skipped;
- with ``AITP_EXPECT_EXPERIMENTAL=1`` (the experimental CI job), a missing
  feature fails the test instead, so a broken experimental build can't pass
  by skipping. This mirrors aitp-rs ``bindings/aitp-py/tests/_experimental.py``.

Presence comes from ``agents/base/sdk_features.has``, the same check the
agent routes use. Tests that *simulate* absence (deleting an attribute)
must not carry the marker: they hold on every wheel.
"""
from __future__ import annotations

import os
from typing import Callable, TypeVar

import pytest

import sdk_features

EXPECT_ENV = "AITP_EXPECT_EXPERIMENTAL"
# Applied instead of a skip in expect mode; tests/conftest.py fails a test
# that carries it.
EXPECTED_MISSING_MARK = "sdk_feature_expected_missing"
SKIP_PREFIX = "aitp-sdk built without"

F = TypeVar("F", bound=Callable)


def expect_experimental() -> bool:
    return os.environ.get(EXPECT_ENV) == "1"


def requires_sdk_feature(feature: str) -> Callable[[F], F]:
    """Skip (or, in expect mode, fail) unless the SDK has ``feature``.

    Every marked test also carries ``sdk_feature(<key>)`` on any wheel, so
    ``pytest -m sdk_feature`` lists exactly the marked tests. Raises
    ``KeyError`` at import for a key ``sdk_features`` doesn't know.
    """
    marks = [pytest.mark.sdk_feature(feature)]
    if not sdk_features.has(feature):
        if expect_experimental():
            marks.append(getattr(pytest.mark, EXPECTED_MISSING_MARK)(feature))
        else:
            marks.append(pytest.mark.skip(reason=f"{SKIP_PREFIX} {feature} (experimental)"))

    def apply(test: F) -> F:
        for mark in marks:
            test = mark(test)
        return test

    return apply
