"""Healthcheck + SDK capability endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from ..capabilities import get_capabilities

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/capabilities")
def capabilities() -> dict:
    """Report the feature surface of the installed ``aitp`` SDK wheel.

    Lets clients (and scenarios) discover which optional SDK features
    are available so missing ones degrade cleanly rather than crashing.
    """
    return get_capabilities()
