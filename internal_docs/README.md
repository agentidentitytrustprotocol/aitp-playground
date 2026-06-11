# Internal docs (contributor & build mechanics)

These pages are for people **hacking on this repo** — engine internals,
the agent-worker pattern, LLM wiring, Docker builds, and the test suite.
They are deliberately **not published to the docs website** (the site syncs
only `docs/**` and `README.md`), so they can stay close to the code and
assume repo context.

For the reader-facing docs — what the playground is, how to run it, how it
drives the protocol — see [`../docs/`](../docs/README.md).

## Pages

| Page | What's in it |
| --- | --- |
| [runner.md](runner.md) | `ScenarioRunner.run()` lifecycle, every workflow step type, the full event taxonomy, cancellation. |
| [agents.md](agents.md) | The agent-worker pattern, the bootstrap contract, adding a capability, adding a framework adapter. |
| [llm-providers.md](llm-providers.md) | OpenAI/Anthropic selection, the stub fallback, async behavior, adding a provider. |
| [docker.md](docker.md) | The multi-stage Dockerfile, the compose files, and the Dockerized e2e build. |
| [testing.md](testing.md) | Unit / integration / protocol-e2e / live-LLM tiers and how each is gated. |

## Why split this out

The published site is for someone *learning and using* AITP and the
playground. This folder is for someone *modifying* the playground. Keeping
them apart keeps the site focused and lets these docs go deep without
worrying about a general audience. If a page here would genuinely help a
site reader, promote it into `../docs/` instead of cross-publishing.
