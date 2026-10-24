# Security

## Reporting a vulnerability

Report privately through GitHub's **Security → Report a vulnerability** on
this repository. Please do not open a public issue for a security problem.

## Authentication

| Route | How it is authenticated |
| --- | --- |
| `GET /healthz`, `/capabilities`, `/packs`, `/scenarios*`, `/dashboard` (HTML shell), `/docs`, `/redoc`, `/openapi.json` | Public: static content only |
| Every other route (runs, run detail and SSE streams, hosted agents, `/cp/*`, `/agents`, `/metrics`) | `Authorization: Bearer <PLAYGROUND_API_KEY>` |
| `POST /webhooks/cp/{run_id}` | HMAC-SHA256 of the body under the per-webhook secret CP issued (`X-Aitp-Signature`). The signed `enqueuedAt` must be within 5 minutes of now, and a `deliveryId` already recorded on the run is not recorded again |
| `POST /internal/telemetry` | A per-process token the playground writes into each agent's bootstrap file. Agents never receive the API key |

- `PLAYGROUND_API_KEY` is a comma-separated list. To rotate it, add the new
  key, move callers (the console's `PLAYGROUND_API_KEY`) over, then remove
  the old key.
- Keys are compared in constant time against every configured key.
- **Fail closed:** with no key configured, authenticated routes answer `503`.
  They never fall back to open.
- `tests/unit/test_route_auth.py` walks `app.routes` and fails if any route
  is neither guarded nor on the public allowlist above. A new route is
  private until someone adds it to that list on purpose.

## Spend limits

These run before any agent is spawned or any provider is called:

| Limit | Setting | Scope |
| --- | --- | --- |
| Token bucket | `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` | `POST /runs` and `POST /hosted-agents/{id}/invoke`, per client IP and per API key |
| Global hourly cap | `MAX_RUNS_PER_HOUR` | Run creations plus hosted invokes, all callers. The next one gets `429` |
| Per-run token budget | `RUN_TOKEN_BUDGET`, `LLM_MAX_TOKENS_PER_CALL` | Enforced in each agent (`agents/base/llm.py`) before every provider call. Every call is capped at `max_tokens`, and the call that would exceed the budget gets `429` without contacting the provider |

Provider error text is redacted (configured keys, `sk-…` strings and bearer
tokens) before it reaches a response, a log line or telemetry.

These limits bound one service's spend. They do not replace a hard monthly
spend limit set in the OpenAI and Anthropic dashboards; set both.

### Single-replica assumption

The rate limit, the hourly cap and the token budget are all held **in
process memory**. They are correct only while the service runs as **one
replica**: N replicas multiply every limit by N, and a restart resets them.
The Railway service is deployed with one replica. Before scaling out, move
this state to a shared store such as Postgres or Redis.

The per-IP bucket keys on the right-most `X-Forwarded-For` entry, which is
the one Railway's edge proxy appends, so a client cannot choose its own
bucket by sending that header. Requests the console proxies share the
console's egress IP, so the per-key bucket and the hourly cap are what bound
them.
