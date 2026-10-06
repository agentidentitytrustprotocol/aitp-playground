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

`aitp-verifier` is not installable from PyPI (404 as of 2026-08-25). CI *does*
clone the sibling checkout before running this suite (`ci.yml`), specifically
so `test_the_vendored_canonicalizer_has_not_drifted_from_its_source` is a real
gate rather than a developer-machine-only check — but a released PyPI package
would pin a *version*, reintroducing exactly the staleness vendoring avoids
(`PENDING.md` P3). So: a copy, with this note.

If `aitp-verifier` is ever published, delete this file and take a dev-group
dependency on it instead.

This is a stdlib-only module (json, math, typing) and is reproduced verbatim
below apart from this header.

Re-synced 2026-09-25 against `aitp-verifier-py` commit `5179952` ("depth-cap
JCS canonicalization to stop RecursionError escaping", PR #39): adds
`_MAX_DEPTH` and threads a `depth` counter through `_serialize` so
attacker-supplied nesting — which can sit anywhere inside an `extensions`
member RFC-AITP-0001 §7 forbids inspecting — is a structural `JcsError`
instead of an unguarded `RecursionError` escaping the verifier.

Re-synced 2026-10-06 against `aitp-verifier-py` commit `754417a` (HEAD): adds
`_MAX_NODES_VISITED` and a `visits` counter threaded through `_serialize`
(issue #54), bounding total work for aliased Python values that `_MAX_DEPTH`
alone does not.
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


# Maximum *relative* nesting depth of a single canonicalization walk -- not a
# bound on total document size, which is an orthogonal concern already bounded
# by input size (JCS output is linear in input, and this library parses nothing
# itself: every entry point receives an already-parsed value). Chosen against
# three measured bounds:
#   1. real AITP artifacts nest ~3-5 levels (deepest shapes in the conformance
#      pack: `session_bundle.session_bundle.participants[i]` and
#      `issuer_revocation_list.snapshot.revocation_list.entries[i]`), so 256 is
#      ~50x any legitimate document;
#   2. the interpreter's own ceiling from these call sites is ~993-1200 frames
#      (measured on CPython 3.13 at the stock limit of 1000);
#   3. the cap must leave generous headroom for the *embedding caller's* stack,
#      which is unmeasurable from inside this library -- 256 leaves it ~730 of
#      the interpreter's frame budget.
# Deliberately a fixed constant rather than something derived from
# `sys.getrecursionlimit()` at import time: the rejection must be reproducible
# across interpreters and across a caller that changes the limit, which is what
# makes it testable at all.
_MAX_DEPTH = 256

# Maximum number of _serialize calls ("nodes") a single dumps/canonicalize
# call will make, checked at entry -- before the existing depth check, same
# "guard at entry, not at the two recursion sites" placement _MAX_DEPTH's own
# check already uses. _MAX_DEPTH bounds nesting *depth* only: it does nothing
# for a candidate-free-analogue value (a long flat list of scalars, no
# nesting at all) and, worse, for an *aliased* Python value -- the same
# dict/list object referenced more than once inside its own containing
# structure, unreachable via JSON (which never aliases), reachable only from
# a direct Python caller constructing the value in-process -- the walk is not
# merely unbounded but exponential, the same primitive issue #49 closed for
# jwk.py's list-only walk. Unlike jwk.py, this walk recurses into BOTH dicts
# and lists (jwk.py's dict branch is always a terminal leaf), so the aliasing
# primitive is reachable through either container type here, and this walk
# allocates real output per node (`out.append`), not merely CPU time.
#
# Bounding total node visits also bounds total output size, but as a BOUNDED
# multiplier over the attacker's own real allocated memory, not literally as
# a fixed O(1)-per-visit constant: a revisited dict of width `w` re-sorts its
# keys on every visit (`sorted(value.keys(), ...)` below), costing
# `O(w log w)`, not `O(1)`, per revisit -- negligible at this cap
# (`log(200000) ~= 18`) but worth stating precisely rather than overclaiming
# "fixed." A single non-aliased, oversized leaf (one huge string/number) is
# explicitly out of scope: its own formatting cost is proportional to that
# value's own already-allocated size, the same "document size is bounded by
# input size" cost this module already treats as expected and orthogonal to
# aliasing (see the module docstring) -- this cap bounds the DISPROPORTION
# aliasing introduces, not raw non-aliased document size.
#
# 200000 is generous, not tightly calibrated: no schema-level cap exists on
# array length anywhere this module's callers construct values from
# (revocation entries, manifest files, session participants -- checked; none
# found), nor in the sibling spec repo's own schemas, so there is no real
# observed maximum to calibrate against -- same epistemic status jwk.py's own
# "generous, not tight" constants have. At the cap, measured worst-case cost
# (list-aliased: 56ms CPU / 391KB output; dict-aliased: 132ms / ~1.37MB; flat
# non-aliased width-200000 dict: 192ms / ~3.18MB) is negligible either way for
# a synchronous per-call verification path, while remaining 3-4 orders of
# magnitude over any realistic AITP document (which nests only 3-5 levels
# per _MAX_DEPTH's own reasoning above).
_MAX_NODES_VISITED = 200000


def _visit(visits: list[int]) -> None:
    # `visits` is a single-element list used as a mutable counter box (a bare
    # int can't be mutated across recursive calls by reference), the same
    # shape `out` already has -- and, like `out`, fresh per top-level
    # `dumps`/`canonicalize` call, never shared across separate calls.
    visits[0] += 1
    if visits[0] > _MAX_NODES_VISITED:
        raise JcsError(f"JSON value visits more than {_MAX_NODES_VISITED} nodes while canonicalizing")


def _serialize(value: JsonValue, out: list[str], depth: int = 0, visits: list[int] | None = None) -> None:
    # `visits` defaults to `None` only as a safety net for a direct caller of
    # this private, non-`__all__`-exported function other than `dumps` (none
    # currently exist) -- `dumps` itself always passes a freshly-initialized
    # counter explicitly, the same way it always starts `depth` at 0.
    if visits is None:
        visits = [0]
    # Guard at entry, not at the two recursion sites, and ahead of the depth
    # check below: at the call sites a caller passing an already-deep value
    # could exceed either cap before the first check ran. `depth` defaults to
    # 0 so `dumps` needs no change to its own `depth` handling.
    _visit(visits)
    if depth > _MAX_DEPTH:
        raise JcsError(f"JSON nesting exceeds the maximum canonicalizable depth ({_MAX_DEPTH})")
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
            _serialize(item, out, depth + 1, visits)
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
            _serialize(value[key], out, depth + 1, visits)
        out.append("}")
    else:
        raise JcsError(f"value is not JSON-serializable: {type(value).__name__}")


def dumps(value: JsonValue) -> str:
    """Return the JCS canonical form of *value* as a ``str``."""
    out: list[str] = []
    _serialize(value, out, 0, [0])
    return "".join(out)


def canonicalize(value: JsonValue) -> bytes:
    """Return the JCS canonical form of *value* as UTF-8 bytes."""
    return dumps(value).encode("utf-8")
