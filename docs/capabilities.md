# SDK capabilities & conformance

The `aitp` wheel ships a **core** v0.1 surface (identity, handshake, TCT
verify, delegation, revocation) plus several **experimental** surfaces
gated behind Cargo `experimental-*` features. A given wheel may or may not
expose renewal, session bundles, SPKI pinning, the TCT verification cache,
or multi-hop delegation verification — it depends on how it was built. This
page covers how the playground discovers what's installed and degrades
cleanly, plus the RFC conformance harness.

Source: `src/aitp_playground/capabilities.py`,
`src/aitp_playground/conformance.py`, `src/aitp_playground/api/health.py`.

## Feature detection

`capabilities.py` probes the installed wheel by `hasattr` (the same
convention as `tests/unit/test_sdk_blocked_features.py`) and reports a
stable set of feature keys. The probe **never raises** — if the wheel is
absent (e.g. CI without it), every feature reports `False` and
`sdk_available` is `False`.

| Feature key | Detected by | Backing surface |
| --- | --- | --- |
| `oidc` | `hasattr(aitp, "JwksProvider")` | RFC-AITP-0002 OIDC identity binding |
| `session_bundle` | `hasattr(aitp, "SessionBundleBuilder")` | RFC-AITP-0010 session bundles |
| `spki_pinning` | `hasattr(aitp, "SpkiPinVerifier")` | SPKI client-cert pinning |
| `tct_renewal` | `hasattr(AitpAgent, "build_renewal_request")` | RFC-AITP-0005 §10 in-band renewal |
| `tct_cache` | `hasattr(aitp, "TctStore")` | RFC-AITP-0005 verification cache |
| `multihop_delegation` | `hasattr(aitp, "verify_delegation_multihop")` | RFC-AITP-0011 multi-hop delegation |

The keys are **stable across releases** — scenarios reference them by name
when declaring a required capability, so don't rename them. The probe is
LRU-cached (the installed wheel doesn't change during a process lifetime);
tests that monkeypatch the SDK call `get_capabilities.cache_clear()` to
force a re-probe.

Helpers: `get_capabilities()` (full report dict), `has_feature(name)`,
`sdk_available()`.

## `GET /capabilities`

The probe is exposed so operators can see the wheel's true surface at
runtime:

```bash
curl -s http://localhost:8000/capabilities | jq .
```
```json
{
  "sdk_available": true,
  "version": "0.2.1",
  "features": {
    "oidc": true,
    "session_bundle": false,
    "spki_pinning": true,
    "tct_renewal": true,
    "tct_cache": true,
    "multihop_delegation": false
  }
}
```

`version` comes from `aitp.__version__` if present, otherwise the installed
distribution metadata for `aitp` — the compiled wheel doesn't always set
`__version__`.

## Building a feature-complete wheel

The native build flag controls which experimental surfaces compile in:

```bash
cd ../aitp-rs/bindings/aitp-py
maturin develop --release --features experimental   # enables all experimental-* gates
```

Without `--features experimental` the core surface still works; the
experimental scenarios degrade rather than crash (see below). The Docker
build's `INSTALL_EXTRAS` and feature wiring is in [docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md).
The build flags and what each feature gate turns on are documented by the
SDK itself —
[aitp-rs · sdk-python.md § Build](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#build)
and the
[aitp-py README](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/bindings/aitp-py/README.md).

## Graceful degradation

Scenarios that exercise an experimental surface check `GET /capabilities`
(or the SDK raises) and degrade cleanly when the wheel lacks the feature —
the step records a "feature not available" outcome instead of crashing the
run. This is why you can run the whole scenario catalog against a
core-only wheel and still get a clean event log.

The feature-gated step types and their scenarios:

| Feature | Step types | Demo scenario |
| --- | --- | --- |
| `oidc` | (handshake with an `identity_type: oidc` agent) | `intra-org/oidc-identity` |
| `tct_renewal` | `renew_tct` | `intra-org/tct-renewal` |
| `tct_cache` | `tct_cache_stats` | `intra-org/tct-cache-perf` |
| `session_bundle` | `export_session_bundle`, `verify_session_bundle` | `intra-org/session-bundle` |
| `spki_pinning` | `spki_pin_check` | `intra-org/spki-pinning` |
| `multihop_delegation` | `delegate` / `redeem_delegation` (2-hop) | `intra-org/delegation-multihop` |

See [aitp-integration.md](aitp-integration.md#post-v01-experimental-surfaces)
for where each SDK surface is actually called.

## Conformance harness

`conformance.py` catalogs the RFC conformance fixtures shipped by the specs
repo
([`agentidentitytrustprotocol/schemas/conformance/`](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/tree/main/schemas/conformance),
located as a sibling checkout) and reports which ones the installed wheel
could execute. It's a metadata/readiness report — it does **not** run the
fixtures; it classifies them. (The fixtures are owned by the spec; the
SDK's own pass/fail status against them is the
[aitp-rs conformance matrix](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/conformance-matrix.md).)

Run it from the CLI:

```bash
uv run python -m aitp_playground.cli conformance
# Conformance corpus: /…/agentidentitytrustprotocol/schemas/conformance
#   installed SDK: aitp 0.2.1
#   fixtures: 42  (required for v0.1: 31)
#   by RFC:   {'RFC-AITP-0001': 6, 'RFC-AITP-0005': 9, ...}
#   by tier:  {'core': 31, 'extension': 8, 'draft': 3}
#   wheel readiness: {'core': 31, 'available': 5, 'skipped': 6}
#   ok  fixture metadata valid

uv run python -m aitp_playground.cli conformance --json          # raw report
uv run python -m aitp_playground.cli conformance --fixtures-dir <path>
```

Each fixture carries metadata (`id`, `rfc`, `status`, `required_for_v0_1`,
`feature`). The harness:

- **validates metadata** — required fields present, `status` is one of
  `core`/`draft`/`extension`/`reserved`, `rfc` is `RFC-AITP-####`-shaped,
  and a non-core fixture can't be `required_for_v0_1`. Any violation makes
  the command exit non-zero (CI can gate on a malformed corpus).
- **classifies readiness** per fixture against the installed wheel:
  - `core` — no feature gate; always runnable.
  - `available` — feature-gated and the wheel exposes the feature.
  - `skipped` — feature-gated and the wheel lacks the feature.
  - `unknown-feature` — the fixture names a feature the playground
    doesn't map (`FEATURE_TO_CAPABILITY`).

`build_report()` returns the structured form (`total`,
`required_for_v0_1`, `by_rfc`, `by_tier`, `by_readiness`,
`metadata_errors`, `valid`) — the same dict the `--json` flag prints.

## Where to read next

- What each SDK feature actually does → [aitp-rs · sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md)
- How each experimental surface is wired in the playground → [aitp-integration.md](aitp-integration.md#post-v01-experimental-surfaces)
- Which scenario demonstrates each feature → [scenarios.md](scenarios.md#scenarios-in-the-box)
- Building the wheel in Docker → [docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md)
