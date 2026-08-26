# ruff: noqa
"""RFC 8785 (JCS) canonicalizer — VENDORED COPY. Do not edit here.

Provenance: `aitp-verifier-py/aitp_verifier/jcs.py`, an independent
implementation written from the RFC text and validated against the spec's
pinned `known-answer/jcs-sha256.json` vectors.

**Why this is duplicated instead of imported.** The test that uses it
(`test_revocation_signing_convention.py`) exists to check what the *installed
aitp-sdk wheel* signs. An oracle taken from that same wheel would be circular:
it would agree with the SDK under any self-consistent convention, including a
wrong one — which is exactly how the pre-0.5.0 wrapped-form signing input
survived a full release across this family. The oracle has to be independent
of the artifact under test.

`aitp-verifier` is not installable from PyPI (404 as of 2026-08-25), and a
git/path dependency on a sibling checkout would make this repo's unit suite
depend on a repo that CI does not check out. So: a copy, with this note.

If `aitp-verifier` is ever published, delete this file and take a dev-group
dependency on it instead.

This is a stdlib-only module (json, math, typing) and is reproduced verbatim
below apart from this header.
"""


from __future__ import annotations

import json
import math
from typing import Union, cast

JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]

__all__ = ["JcsError", "canonicalize", "dumps", "loads"]


class JcsError(ValueError):
    """Input cannot be canonicalized or strictly parsed under RFC 8785 rules."""


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    obj: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in obj:
            raise JcsError(f"duplicate object member name: {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> JsonValue:
    raise JcsError(f"non-finite JSON constant not allowed: {name}")


def loads(data: Union[str, bytes]) -> JsonValue:
    """Parse JSON strictly: valid UTF-8, no duplicate members, no NaN/Infinity."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JcsError(f"input is not valid UTF-8: {exc}") from exc
    try:
        return cast(
            JsonValue,
            json.loads(
                data,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            ),
        )
    except json.JSONDecodeError as exc:
        raise JcsError(f"invalid JSON: {exc}") from exc


# --- number formatting (ECMA-262 Number::toString, base 10) ------------------


def _format_number(value: Union[int, float]) -> str:
    if isinstance(value, bool):  # bool is an int subclass — guard first
        raise JcsError("bool is not a number")
    if isinstance(value, int):
        try:
            value = float(value)
        except OverflowError as exc:
            raise JcsError(f"integer magnitude exceeds IEEE 754 range: {value}") from exc
    if math.isnan(value) or math.isinf(value):
        raise JcsError("NaN and Infinity are not valid JSON numbers")
    if value == 0.0:
        return "0"  # normalizes -0.0

    negative = value < 0.0
    magnitude = -value if negative else value
    digits, n = _shortest_digits(magnitude)
    k = len(digits)

    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        exponent = n - 1
        exp_str = f"e+{exponent}" if exponent >= 0 else f"e-{-exponent}"
        body = (digits + exp_str) if k == 1 else (digits[0] + "." + digits[1:] + exp_str)
    return "-" + body if negative else body


def _shortest_digits(magnitude: float) -> tuple[str, int]:
    text = repr(magnitude)
    if "e" in text or "E" in text:
        mantissa, _, exp_part = text.lower().partition("e")
        exp = int(exp_part)
    else:
        mantissa, exp = text, 0
    int_part, _, frac_part = mantissa.partition(".")
    all_digits = int_part + frac_part
    n = len(int_part) + exp
    stripped = all_digits.lstrip("0")
    n -= len(all_digits) - len(stripped)
    digits = stripped.rstrip("0")
    if not digits:
        raise JcsError("internal: zero reached digit extraction")
    return digits, n


# --- string + structural serialization ---------------------------------------

_NAMED_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _format_string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        named = _NAMED_ESCAPES.get(code)
        if named is not None:
            out.append(named)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _serialize(value: JsonValue, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_format_string(value))
    elif isinstance(value, (int, float)):
        out.append(_format_number(value))
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        first = True
        # RFC 8785 §3.2.3: sort member names by UTF-16 code units.
        for key in sorted(value.keys(), key=lambda k: k.encode("utf-16-be")):
            if not isinstance(key, str):
                raise JcsError(f"object member name is not a string: {key!r}")
            if not first:
                out.append(",")
            first = False
            out.append(_format_string(key))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise JcsError(f"value is not JSON-serializable: {type(value).__name__}")


def dumps(value: JsonValue) -> str:
    """Return the JCS canonical form of *value* as a ``str``."""
    out: list[str] = []
    _serialize(value, out)
    return "".join(out)


def canonicalize(value: JsonValue) -> bytes:
    """Return the JCS canonical form of *value* as UTF-8 bytes."""
    return dumps(value).encode("utf-8")
