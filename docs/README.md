# aitp-playground — Documentation

A guide to the playground service for contributors. Audience: engineers
building or extending this repo. For a 30-second pitch, see the top-level
[README.md](../README.md); for invariants and sibling-repo pointers see
[CLAUDE.md](../CLAUDE.md).

## Map

| Page | Read this when… |
| --- | --- |
| [architecture.md](architecture.md) | You want the big picture — components, runtime topology, and where AITP lives. Start here. |
| [getting-started.md](getting-started.md) | You're cloning the repo and need it running locally. |
| [scenarios.md](scenarios.md) | You want to author a new scenario, or understand the YAML schema. |
| [agents.md](agents.md) | You're changing an agent worker, adding a new framework, or wiring a new capability. |
| [aitp-integration.md](aitp-integration.md) | You want to know where the SDK is called, how identity/handshake/TCT/revocation work end-to-end. |
| [runner.md](runner.md) | You're working on the engine — step types, execution model, trust scoping, event stream. |
| [llm-providers.md](llm-providers.md) | You're switching LLM providers or adding a new one. |
| [docker.md](docker.md) | You're building images, debugging the multi-stage Dockerfile, or running the Dockerized e2e suite. |
| [testing.md](testing.md) | You're writing or running tests — unit, integration, scenario, or live LLM e2e. |

## Conventions

- `aitp` always means the Python SDK shipped from
  `aitp-rs/bindings/aitp-py`. All AITP protocol logic — keygen, manifests,
  handshake, TCT verify, delegation — goes through it. This service
  **never reimplements** protocol logic; if you find yourself wanting to
  parse a TCT, that's a bug.
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
