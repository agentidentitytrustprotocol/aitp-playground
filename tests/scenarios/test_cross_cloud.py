"""End-to-end cross-cloud (did:web) scenario — disabled by default."""
from __future__ import annotations

import pytest

pytest.importorskip("aitp")
pytestmark = pytest.mark.skip(reason="Live e2e — enable with AITP_E2E=1")
