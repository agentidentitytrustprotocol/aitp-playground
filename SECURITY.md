# Security

## Reporting a vulnerability

Report privately through GitHub's **Security → Report a vulnerability** on
this repository. Please do not open a public issue for a security problem.

## Authentication

The playground key is accepted as `x-api-key: <PLAYGROUND_API_KEY>` (canonical)
or `Authorization: Bearer <PLAYGROUND_API_KEY>` (compatibility). Both go
through one matcher (`src/aitp_playground/security.py`).

### Backend lockdown: `AITP_PLAYGROUND_REQUIRE_API_KEY`

The lockdown is staged by one flag, off by default:

- **`false`** (default): today's per-route auth, in the table below. The
  static listings stay public.
- **`true`**: `ApiKeyMiddleware` answers `401` (`WWW-Authenticate: ApiKey
  header="x-api-key"`) to every request without a valid key before routing,
  including the static listings, the OpenAPI docs, SSE streams, unknown paths
  and any route added later. A websocket is closed with `1008`. The only
  exceptions are:
  - `GET /healthz`;
  - `POST /webhooks/cp/{run_id}`, which has its own HMAC check;
  - `/internal/*`, but only from a direct loopback peer with no
    `X-Forwarded-For` or `Forwarded` header. Agents reach it over
    `PLAYGROUND_INTERNAL_URL` (default `http://127.0.0.1:8000`) with their own
    token;
  - `OPTIONS`.

  With the flag on and no `PLAYGROUND_API_KEY`, the service **refuses to
  start**.

Rollout: callers (the console) send the key while the flag is off, and the
flag is turned on only after that's verified. A wrong key presented while the
flag is off is logged (never its value) and otherwise ignored on public routes.

### Per-route auth (flag off)

| Route | How it is authenticated |
| --- | --- |
| `GET /healthz`, `/capabilities`, `/packs`, `/scenarios*`, `/dashboard` (HTML shell), `/docs`, `/redoc`, `/openapi.json` | Public: static content only (gated when the flag is on) |
| Every other route (runs, run detail and SSE streams, hosted agents, `/cp/*`, `/agents`, `/metrics`) | The playground key, `x-api-key` or Bearer |
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
  private until someone adds it to that list on purpose. The same walk checks
  that every route except the exemptions answers the gate's `401` with the
  flag on.

### Client IP

A caller holding the key (the console) may send the visitor's address in
`X-AITP-Client-IP`. It is trusted **only** on a request carrying a valid key;
without one the header is ignored, so nobody can choose their own rate-limit
bucket. The first comma-separated value is used and normalised to one IPv4
address or one IPv6 /64 (`2001:db8:1:2::/64`); an IPv4-mapped address counts as
its IPv4 address, and a value that isn't a bare IP (`host:port`, `[v6]`, a
hostname) is ignored. This is the same normalisation as `aitp-docs`'
`normalizeClientIp`.

That trusted address is the subject of the per-IP limits and quotas. It is
kept on the run record (and in the SQLite history when `RUN_HISTORY_DB` is set,
for as long as that file keeps the run), never returned by any API. A keyless
request, or a keyed one without the header, has no subject IP: on Railway every
keyless caller appears as the edge proxy, so a per-IP limit there would throttle
every visitor together.

## Spend limits

These run before any agent is spawned or any provider is called, cheapest
first: token buckets, then concurrency, then the per-IP window, then the
global hourly cap. A launch refused at an earlier step never uses up a later
one. The run-limit `429`s carry `{code, limit, retry_after, detail}` and a
`Retry-After` header:

| Limit | Setting | Scope |
| --- | --- | --- |
| Token bucket | `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` | `POST /runs` and `POST /hosted-agents/{id}/invoke`, per client IP (the subject IP when sent, else the proxy-appended address) and per API key. The per-key bucket is skipped when a subject IP is sent: that key (the console) fronts many visitors, and the per-IP limits take over |
| Per-IP launch window | `RUN_RATE_LIMIT_PER_IP`, `RUN_RATE_LIMIT_WINDOW_SECONDS` | `POST /runs` and hosted invokes per trusted client IP (5 per 10 minutes). Only a caller with a subject IP (above) is counted; the next gets `429 run_rate_limited` |
| Concurrency cap | `MAX_CONCURRENT_RUNS` | Runs in progress, service-wide (3). The next launch gets `429 max_concurrent_runs`, `Retry-After: 10`. The slot frees when the run's task ends; a cancel does not free it early, because the agents may still be stopping |
| Run deadline | `RUN_DEADLINE_SECONDS` | A run past 900 s ends `failed` (`run_deadline_exceeded`) and its agents are torn down, so a stuck run can't hold a slot |
| Global hourly cap | `MAX_RUNS_PER_HOUR` | Run creations plus hosted invokes, all callers. The next one gets `429` |
| LLM quota | `LLM_QUOTA_PER_IP_PER_DAY` / `_PER_MONTH`, `LLM_QUOTA_GLOBAL_PER_DAY` / `_PER_MONTH`, `LLM_QUOTA_STORE`, `DATABASE_URL` | Real LLM call units, charged by each agent's quota check before the provider call (30/day and 300/month per trusted client IP; 1000/day and 10000/month overall). Over quota the capability runs on its deterministic mock and the run is flagged (`llm_quota_exceeded`, `llm_quota.resets_at`, one `llm.quota_exceeded` event). It fails closed: if the quota store is unreachable, every real call is refused. Keyless runs charge the global counters only |
| Per-run token budget | `RUN_TOKEN_BUDGET`, `LLM_MAX_TOKENS_PER_CALL` | Enforced in each agent (`agents/base/llm.py`) before every provider call. Every call is capped at `max_tokens`, and the call that would exceed the budget gets `429` without contacting the provider |

Provider error text is redacted (configured keys, `sk-…` strings and bearer
tokens) before it reaches a response, a log line or telemetry.

These limits bound one service's spend. They do not replace a hard monthly
spend limit set in the OpenAI and Anthropic dashboards; set both.

### Single-replica assumption

The rate limit, the hourly cap, the run limits and the token budget are all held **in
process memory**. They are correct only while the service runs as **one
replica**: N replicas multiply every limit by N, and a restart resets them.
The Railway service is deployed with one replica. Before scaling out, move
this state to a shared store such as Postgres or Redis.

The LLM quota is the exception: its counters live in the dedicated playground
Postgres (`DATABASE_URL`), so they are shared by every replica and survive a
restart. The `memory` store is for local runs only.

The quota check (`POST /internal/llm-quota`) trusts the agents the playground
launched. It is reachable only from loopback, with no forwarding headers, and
only with the telemetry token every agent of this process holds. The token is
not bound to one run: an agent holding it could name another run id (charging
that run's IP) or send no run id (charging the global counters only). The
caller can't pick an IP, though: the IP always comes from the run record. The
quota bounds what honest agents spend. It is not a boundary against a
compromised agent, which already holds a provider key.

The per-IP token bucket keys on the trusted subject IP when a keyed caller
sends one (above). Otherwise it keys on the right-most `X-Forwarded-For` entry
(normalised as above), which is the one Railway's edge proxy appends, so a
client cannot choose its own bucket by sending that header. A keyed request
without the header (the console today) shares the console's egress IP, so the
per-key bucket, the concurrency cap and the hourly cap are what bound it.
