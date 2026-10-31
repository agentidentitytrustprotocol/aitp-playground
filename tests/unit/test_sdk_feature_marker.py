"""#117 Phase 3: the ``requires_sdk_feature`` marker's modes.

Presence is simulated through ``sdk_features.has`` so these hold on any
wheel (including the stable-only meta-test subprocess).
"""
from __future__ import annotations

import types

import pytest

import sdk_features
from tests import _sdk_features as marker
from tests import conftest


def _marks(decorated) -> list[pytest.Mark]:
    return list(getattr(decorated, "pytestmark", []))


def _decorate(monkeypatch, *, present: bool, expect: bool):
    monkeypatch.setattr(sdk_features, "has", lambda _feature: present)
    if expect:
        monkeypatch.setenv(marker.EXPECT_ENV, "1")
    else:
        monkeypatch.delenv(marker.EXPECT_ENV, raising=False)

    def test_x() -> None: ...

    return _marks(marker.requires_sdk_feature("tct_renewal")(test_x))


@pytest.mark.parametrize("expect", [False, True])
def test_present_feature_only_tags(monkeypatch, expect) -> None:
    marks = _decorate(monkeypatch, present=True, expect=expect)
    assert [(m.name, m.args) for m in marks] == [("sdk_feature", ("tct_renewal",))]


def test_absent_feature_skips(monkeypatch) -> None:
    marks = {m.name: m for m in _decorate(monkeypatch, present=False, expect=False)}
    assert set(marks) == {"sdk_feature", "skip"}
    assert marks["skip"].kwargs["reason"] == "aitp-sdk built without tct_renewal (experimental)"


def test_absent_feature_in_expect_mode_is_marked_to_fail(monkeypatch) -> None:
    marks = {m.name: m for m in _decorate(monkeypatch, present=False, expect=True)}
    assert set(marks) == {"sdk_feature", marker.EXPECTED_MISSING_MARK}
    assert marks[marker.EXPECTED_MISSING_MARK].args == ("tct_renewal",)


def test_expect_mode_needs_exactly_1(monkeypatch) -> None:
    monkeypatch.setenv(marker.EXPECT_ENV, "true")
    assert marker.expect_experimental() is False
    monkeypatch.setenv(marker.EXPECT_ENV, "1")
    assert marker.expect_experimental() is True


def test_unknown_feature_fails_at_decoration() -> None:
    with pytest.raises(KeyError):
        marker.requires_sdk_feature("renewal")


def test_conftest_fails_a_test_marked_expected_missing() -> None:
    mark = pytest.mark.sdk_feature_expected_missing("session_bundle").mark
    item = types.SimpleNamespace(
        get_closest_marker=lambda name: mark if name == mark.name else None,
    )
    with pytest.raises(pytest.fail.Exception, match="lacks session_bundle but AITP_EXPECT_EXPERIMENTAL=1"):
        conftest.pytest_runtest_setup(item)


def test_conftest_leaves_other_tests_alone() -> None:
    item = types.SimpleNamespace(get_closest_marker=lambda _name: None)
    conftest.pytest_runtest_setup(item)
