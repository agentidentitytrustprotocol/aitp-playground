"""pytest plugin: run the suite as if aitp-sdk were a stable-only wheel.

From aitp-sdk 0.24 the default wheel lacks the ``experimental`` surfaces.
This plugin deletes them from the installed (full) wheel before any test
module is imported, so ``tests/integration/test_stable_only_sdk.py`` can
prove the suite passes there, marked tests skipping, without a second wheel.

Load it ONLY in a subprocess:
``python -m pytest tests/unit -p tests._stable_only_sdk``. Loaded in-process
it would strip the parent session's real ``aitp``.
"""
from __future__ import annotations

# (owner attribute on the aitp module or None for the module, name)
STRIPPED: tuple[tuple[str | None, str], ...] = (
    ("AitpAgent", "build_renewal_request"),
    ("AitpAgent", "process_renewal_request"),
    (None, "SessionBundleBuilder"),
    (None, "verify_session_bundle"),
    (None, "verify_delegation_multihop"),
)


def pytest_configure(config) -> None:
    # Imported here, not at module top: -p plugins load before the
    # pythonpath ini option applies.
    import aitp

    for owner_name, name in STRIPPED:
        owner = aitp if owner_name is None else getattr(aitp, owner_name)
        if hasattr(owner, name):
            delattr(owner, name)
        if hasattr(owner, name):
            raise RuntimeError(
                f"could not delete aitp.{owner_name + '.' if owner_name else ''}{name}: "
                "the stable-only simulation no longer works on this wheel. "
                "Build a genuine stable-only wheel from aitp-rs main instead."
            )

    from aitp_playground import capabilities

    capabilities.get_capabilities.cache_clear()
