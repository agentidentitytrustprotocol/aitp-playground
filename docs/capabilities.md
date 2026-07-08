# SDK capabilities & conformance

The `aitp` wheel (PyPI distribution `aitp-sdk`) ships a **core** surface
(identity, handshake, TCT verify, delegation, revocation) plus several
post-v0.1 surfaces — renewal, session bundles, SPKI pinning, the TCT
verification cache, multi-hop delegation verification. Since `aitp-sdk`
0.4.0 **all of these ship by default** on the published wheel; only an
older 0.3.x wheel or a custom `--no-default-features` build omits some.
The playground therefore probes the *installed* wheel at runtime rather
than assuming. This page covers that probe, how scenarios degrade
cleanly, and the RFC conformance harness.

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
| `tct_renewal` | `hasattr(AitpAgent, "build_renewal_request")` | RFC-AITP-0013 / RFC-AITP-0004 §8.1 in-band renewal |
| `tct_cache` | `hasattr(aitp, "TctStore")` | SDK-side cache for RFC-AITP-0005 TCT verification |
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
  "version": "0.4.0",
  "features": {
    "oidc": true,
    "session_bundle": true,
    "spki_pinning": true,
    "tct_renewal": true,
    "tct_cache": true,
    "multihop_delegation": true
  }
}
```

`version` comes from `aitp.__version__` if present, otherwise the installed
distribution metadata for `aitp-sdk` (falling back to a bare `aitp` dist) —
the compiled wheel doesn't always set `__version__`.

## Getting a feature-complete wheel

Nothing special: the PyPI wheel is feature-complete. `uv sync` installs
`aitp-sdk` with the full default surface, and a plain source build
(`maturin develop --release` in `aitp-rs/bindings/aitp-py`) compiles the
same defaults. A slimmed-down wheel only appears if someone builds with
`--no-default-features` — the probe above is what keeps that (or an old
0.3.x wheel) from crashing scenarios. The Docker build compiles the wheel
from the sibling `aitp-rs` source; its `INSTALL_EXTRAS` wiring is in
[docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md).
The Cargo feature gates and what each one turns on are documented by the
SDK itself —
[aitp-rs · sdk-python.md § Build](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#build)
and the
[aitp-py README](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/bindings/aitp-py/README.md).

## Graceful degradation

Scenarios that exercise a feature-gated surface check `GET /capabilities`
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
[aitp-rs conformance matrix](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/conformance.md#v02-conformance-matrix).)

Run it from the CLI:

```bash
uv run python -m aitp_playground.cli conformance
# Conformance corpus: /…/agentidentitytrustprotocol/schemas/conformance
#   installed SDK: aitp 0.4.0
#   fixtures: 53  (required for v0.1: 1)
#   by RFC:   {'RFC-AITP-0001': 3, 'RFC-AITP-0004': 11, 'RFC-AITP-0005': 10, ...}
#   by tier:  {'core': 46, 'draft': 7}
#   wheel readiness: {'available': 7, 'core': 46}
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
- How each post-v0.1 surface is wired in the playground → [aitp-integration.md](aitp-integration.md#post-v01-experimental-surfaces)
- Which scenario demonstrates each feature → [scenarios.md](scenarios.md#scenarios-in-the-box)
- Building the wheel in Docker → [docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md)
