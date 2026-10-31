# SDK capabilities & conformance

The `aitp` wheel (PyPI distribution `aitp-sdk`) ships a **core** surface
(identity, handshake, TCT verify, delegation, revocation) plus several
draft and extension surfaces — renewal, session bundles, SPKI pinning, the TCT
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
  "version": "0.19.0",
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

`version` (yours will differ — it is whatever `uv.lock` resolved inside the
`pyproject.toml` range, currently `>=0.19.0,<0.20`; the `aitp/0.3` wire floor is 0.17) comes from `aitp.__version__` if present, otherwise the installed
distribution metadata for `aitp-sdk` (falling back to a bare `aitp` dist) —
the compiled wheel doesn't always set `__version__`.

## Getting a feature-complete wheel

From `aitp-sdk` 0.24 the PyPI wheels leave out the `experimental`
features: TCT renewal, session bundles and multi-hop delegation. Plain
`uv sync` installs that default wheel, so on it the
`intra-org/tct-renewal` and `intra-org/session-bundle` scenarios refuse
cleanly (see below). To get all three, compile the locked sdist with the
feature:

```bash
uv sync --locked --all-extras --no-binary-package aitp-sdk \
  --config-settings-package 'aitp-sdk:build-args=--features experimental'
uv run --no-sync uvicorn aitp_playground.main:app --port 8000
```

`--no-sync` matters on every later `uv run`: a plain `uv run` re-syncs the
default wheel over the source build. The compile needs a Rust toolchain.

The published image and the compose stacks already do this
(`AITP_SDK_SOURCE=pypi-src`, `AITP_SDK_FEATURES=experimental`; DECISIONS.md
D-26). A plain `docker build` keeps the default PyPI wheel
(`AITP_SDK_SOURCE=pypi`), and compiling the sibling `aitp-rs` source is
`AITP_SDK_SOURCE=path`. The Dockerfile wiring is in [docker.md](docker.md).
The Cargo feature gates and what each one turns on are documented by the
SDK itself —
[aitp-rs · sdk-python.md § Build](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md#build)
and the
[aitp-py README](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/bindings/aitp-py/README.md).

## Graceful degradation

From `aitp-sdk` 0.24 (aitp-rs v0.24.0), renewal, session bundles and
multi-hop delegation are opt-in build features (`experimental`), so a
default wheel lacks them. The playground handles a missing feature at two
levels:

- **Run preflight.** A scenario's SDK requirements are derived from its
  workflow step types (`STEP_SDK_FEATURES` in
  `src/aitp_playground/capabilities.py`), after any template is applied;
  scenarios never declare them. If the installed wheel lacks one,
  `ScenarioRunner.run` refuses the run before spawning any agent:
  `POST /runs` still returns 202, and the run ends `failed` with one
  `run.failed` event carrying `code: "sdk_feature_unavailable"` and an
  error such as
  `sdk_feature_unavailable: intra-org/tct-renewal@1.0.0 needs tct_renewal; the installed aitp-sdk 0.24.0 lacks it`.
  There is no `run.started`. A refused run still counts against
  `MAX_RUNS_PER_HOUR`, like the other preflight failures (unknown scenario,
  invalid inputs).
- **Discovery.** `GET /scenarios`, `GET /scenarios/{ref}` (and each of its
  templates), `GET /scenarios/{ref}/templates` and
  `GET /scenarios/{ref}/templates/{name}` report `sdk_features_required`
  (sorted list) and `sdk_available` (bool), so a client can grey out what
  this deployment can't run. `cli validate` prints the requirement as a
  warning; `cli dry-run` exits 1 when a feature is missing.

The step types that need an optional feature, and their scenarios. Only
the rows marked *gated* are refused at preflight; the others are probed
for reporting only:

| Feature | Step types | Demo scenario | Preflight |
| --- | --- | --- | --- |
| `oidc` | (handshake with an `identity_type: oidc` agent) | `intra-org/oidc-identity` | stable |
| `tct_renewal` | `renew_tct` | `intra-org/tct-renewal` | *gated* |
| `tct_cache` | `tct_cache_stats` | `intra-org/tct-cache-perf` | stable |
| `session_bundle` | `export_session_bundle`, `verify_session_bundle` | `intra-org/session-bundle` | *gated* |
| `spki_pinning` | `spki_pin_check` | `intra-org/spki-pinning` | stable |
| `multihop_delegation` | `delegate` / `redeem_delegation` (2-hop) | `intra-org/delegation-multihop` | stable (the scenario redeems hop by hop; `verify_delegation_multihop` is not on its path) |

`tct_renewal` reports present only when the wheel has **both**
`AitpAgent.build_renewal_request` and `process_renewal_request`.

See [aitp-integration.md](aitp-integration.md#draft-and-extension-surfaces)
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
#   installed SDK: aitp 0.19.0
#   fixtures: 72  (required for v0.1: 1)
#   by RFC:   {'RFC-AITP-0001': 5, 'RFC-AITP-0004': 11, 'RFC-AITP-0005': 12, ...}
#   by tier:  {'core': 62, 'draft': 10}
#   wheel readiness: {'available': 10, 'core': 62}
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
- How each draft/extension surface is wired in the playground → [aitp-integration.md](aitp-integration.md#draft-and-extension-surfaces)
- Which scenario demonstrates each feature → [scenarios.md](scenarios.md#scenarios-in-the-box)
- Which SDK the Docker image contains (PyPI wheel vs sibling source) → [docker.md](docker.md)
