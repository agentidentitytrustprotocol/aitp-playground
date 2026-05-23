"""Trust resolver helpers."""
from __future__ import annotations

from aitp_playground.trust.resolver import encode_did_web


def test_encode_did_web_quotes_colon() -> None:
    did = encode_did_web("localhost:8101")
    assert did == "did:web:localhost%3A8101"
