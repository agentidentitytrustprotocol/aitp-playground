"""Healthcheck + SDK capability endpoints."""
from __future__ import annotations

import os

from fastapi import APIRouter

from ..capabilities import get_capabilities

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    """Liveness plus the git commit the image was built from.

    ``AITP_PLAYGROUND_REVISION`` is baked in by the Dockerfile from the
    ``GIT_SHA`` build-arg. A release retags the main-built image without
    rebuilding it, so this equals the release commit; release.yml falls back
    to it when Railway does not report the running image digest (D-33).
    """
    return {"status": "ok", "revision": os.environ.get("AITP_PLAYGROUND_REVISION") or "unknown"}


@router.get("/capabilities")
def capabilities() -> dict:
    """Report the feature surface of the installed ``aitp`` SDK wheel.

    Lets clients (and scenarios) discover which optional SDK features
    are available so missing ones degrade cleanly rather than crashing.
    """
    return get_capabilities()
