"""The API key the test suite runs under (set in ``tests/conftest.py``).

Clients that exercise authenticated routes pass ``headers=AUTH``; tests
that check the auth boundary itself build a client without it.
"""
from __future__ import annotations

TEST_API_KEY = "test-playground-key-not-a-secret"
AUTH = {"Authorization": f"Bearer {TEST_API_KEY}"}
