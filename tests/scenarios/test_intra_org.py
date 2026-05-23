"""End-to-end intra-org scenario — needs aitp SDK + ability to spawn subprocesses."""
from __future__ import annotations

import pytest

pytest.importorskip("aitp")
pytestmark = pytest.mark.skip(reason="Live e2e — enable with AITP_E2E=1 after maturin develop")
