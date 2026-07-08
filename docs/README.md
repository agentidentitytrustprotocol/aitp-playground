# aitp-playground — Documentation

What the playground is, how to run it, and how it drives the **Agent
Identity & Trust Protocol** end-to-end. For a 30-second pitch see the
top-level [README.md](../README.md); for the protocol and SDK themselves
see [external references](#external-references-the-source-of-truth-lives-elsewhere)
below.

## Map

| Page | Read this when… |
| --- | --- |
| [architecture.md](architecture.md) | You want the big picture — components, runtime topology, and where AITP lives. Start here. |
| [getting-started.md](getting-started.md) | You're cloning the repo and need it running locally — install, env, endpoints, CLI, and the development & testing workflow (test tiers, ruff, CI). |
| [scenarios.md](scenarios.md) | You want to author a new scenario, or understand the YAML schema. |
| [aitp-integration.md](aitp-integration.md) | You want to know where the SDK is called, how identity/handshake/TCT/revocation work end-to-end, and the post-v0.1 surfaces (OIDC, renewal, bundles, pinning, multi-hop). |
| [observability.md](observability.md) | You want events, the SSE stream, narration, Prometheus metrics, the dashboard, or run persistence. |
| [control-plane.md](control-plane.md) | You're wiring the optional Control Plane — discovery, enrollment, revocation, webhooks, trust anchors, observability proxies. |
| [capabilities.md](capabilities.md) | You want to know which SDK features the installed wheel exposes, how scenarios degrade, and the conformance harness. |

### Contributor & build docs (in the repo, not on the docs site)

Deeper internals and ops mechanics live under
[`internal_docs/`](https://github.com/agentidentitytrustprotocol/aitp-playground/tree/main/internal_docs)
— they're aimed at people hacking on the repo, not at a reader learning
the project, so they're excluded from the published site:

| Page | Read this when… |
| --- | --- |
| [runner.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/runner.md) | You're working on the engine — step types, execution model, trust scoping, event stream. |
| [agents.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/agents.md) | You're changing an agent worker, adding a new framework, or wiring a new capability. |
| [llm-providers.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/llm-providers.md) | You're switching LLM providers or adding a new one. |
| [docker.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/docker.md) | You're building images, debugging the multi-stage Dockerfile, or running the Dockerized e2e suite. |
| [testing.md](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/internal_docs/testing.md) | You're writing or running tests — unit, integration, scenario, or live LLM e2e. |

## External references (the source of truth lives elsewhere)

These docs cover **the playground only** — how it orchestrates scenarios,
spawns agents, and drives the protocol. They deliberately **do not restate
the protocol, the SDK API, or the Control Plane API**; those are owned by
the sibling repos and this is where to read them. Where a page here touches
one of those topics, it summarizes what the playground *does* and links out
for the *normative* detail. If a page here disagrees with one of these, the
sibling wins.

| For… | Go to |
| --- | --- |
| The protocol itself (normative) | [AITP RFC index](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/rfcs/README.md) — handshake (0004), identity (0002), manifest (0003), TCT (0005), delegation (0006), key resolution (0007), revocation (0008), session bundle (0010), multi-hop (0011), renewal (0013) |
| Consuming a peer-issued TCT; reading order for building a peer | [Integration Guide](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/docs/integration-guide.md) · [Implementer Quickstart](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/docs/implementer-quickstart.md) · [Glossary](https://github.com/agentidentitytrustprotocol/agentidentitytrustprotocol/blob/main/docs/GLOSSARY.md) |
| The `aitp` Python SDK API the agents call | [aitp-rs · sdk-python.md](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/sdk-python.md) — every call, with RFC sections and feature flags |
| SDK conformance status | [aitp-rs · conformance.md § v0.2 conformance matrix](https://github.com/agentidentitytrustprotocol/aitp-rs/blob/main/docs/conformance.md#v02-conformance-matrix) |
| The Control Plane HTTP API, events, data model | [aitp-control-plane · docs](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/README.md) — [api.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/api.md) · [events.md](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/events.md) · [integration contract](https://github.com/agentidentitytrustprotocol/aitp-control-plane/blob/main/docs/integration-playground.md) |

## Conventions

- `aitp` always means the Python SDK built from
  `aitp-rs/bindings/aitp-py` and published to PyPI under the
  distribution name `aitp-sdk` (the import name stays `aitp`). All AITP
  protocol logic — keygen, manifests, handshake, TCT verify, delegation —
  goes through it. This service **never reimplements** protocol logic;
  if you find yourself wanting to parse a TCT, that's a bug.
- `aitp-playground` is the service in this repo. It orchestrates
  scenarios, hosts agents, and exposes a small HTTP API.
- "Agent" is overloaded:
  - **Agent worker** — an OS subprocess running on a unique port. Each
    one is its own FastAPI app and owns an `aitp.AitpAgent` identity.
  - **Agent record** — `RunningAgent` dataclass tracked by the
    supervisor.
  - **Agent manifest** — the YAML file describing how to spawn one.
- "Scenario" = one YAML file at
  `scenarios/<pack>/<scenario>/<version>/scenario.yaml`, addressed by
  the ref `<pack>/<scenario>@<version>`.

If you spot something here that disagrees with the code, the code wins —
please update the doc.
