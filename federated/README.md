# Federated (cross-domain) test stacks

Two **separate** playground services on **distinct domains**, so an agent hosted
by org-A establishes AITP identity + trust with an agent hosted by org-B across
a real origin boundary — resolved via `did:web`, not a same-process shortcut.

This is the "is two services on two domains the true test?" answer: **yes, for
resolution + isolation fidelity** — and these stacks provide it. The single-
process scenario suite can't exercise cross-origin `did:web` resolution or prove
no in-process state leaks trust; these do.

## What it proves

- org-B hosts the **analyzer** at its own origin, advertising a `did:web`
  identity (`did:web:org-b.aitp.test` at Level 2; at Level 1 the agent port is
  part of the host, so `did:web:org-b.aitp.test%3A9100`) and serving its own `did.json`,
  manifest, handshake, and capability endpoints there.
- org-A hosts the **researcher**, resolves the analyzer's DID **across the
  boundary**, and runs a real AITP handshake → TCT → capability call.
- **Fail-closed:** if a DID resolves back to `localhost`/`127.*`, the
  handshake is refused (HTTP 409); so is a DID whose resolved origin doesn't
  match the DID's own host. A green run can't be a disguised same-process
  handshake. (Only `localhost` and `127.*` are caught — not `::1` or
  `0.0.0.0`; this is a test-stack guard, not a general SSRF defense.)

| Level | Transport | did:web resolution exercised | Certs |
|-------|-----------|------------------------------|-------|
| **1** | HTTP over `*.aitp.test` hostnames | cross-origin fetch, no TLS | none |
| **2** | HTTPS via Caddy per domain | the real `https` branch + chain validation | local CA (`gen-ca.sh`) |

## Topology

```
        host: pytest (tests/e2e_federated)
          │  control API (http)          │
          ▼ :18000                       ▼ :18001
   ┌──────────────┐               ┌──────────────┐
   │   org-A       │  did:web +   │   org-B       │
   │  researcher   │ handshake +  │  analyzer     │
   │ (initiator)   │──capability─▶│ (responder)   │
   └──────────────┘  over the     └──────────────┘
     org-a.aitp.test  docker net    org-b.aitp.test
                     (L2: via Caddy TLS on :443)
```

Inter-service (agent↔agent) traffic never leaves the docker network and is
addressed by hostname alias. Only the control API is published to the host.

## Run it

### Level 1 — HTTP

```bash
docker compose -f federated/docker-compose.federated.yml up --build -d
AITP_FEDERATED_E2E=1 uv run pytest tests/e2e_federated/ -v
docker compose -f federated/docker-compose.federated.yml down
```

### Level 2 — HTTPS (local CA)

```bash
./federated/gen-ca.sh                       # mint CA + server certs (re-run if your certs predate the SKI/AKI fix)
docker compose -f federated/docker-compose.federated-tls.yml up --build -d
AITP_FEDERATED_E2E=1 uv run pytest tests/e2e_federated/ -v
docker compose -f federated/docker-compose.federated-tls.yml down
```

The e2e suite is identical for both levels — only the compose file changes.

**Regenerate old certs.** The root CA now carries `basicConstraints`,
`keyUsage` and a `subjectKeyIdentifier` (#86); without them Python 3.13 /
OpenSSL 3 rejects the chain with "Missing Authority Key Identifier" (surfacing
as a 502 from `resolve-and-handshake`). Certs minted before that fix must be
re-generated with `gen-ca.sh`. The trust bundle it writes appends the test CA
to the host's `/etc/ssl/cert.pem` when present (macOS); on hosts without that
file the bundle holds only the test CA, which replaces the container's system
trust, so other outbound HTTPS from the container will fail.

**Service names differ by level.** Level 1 runs `org-a` / `org-b`; Level 2 runs
`pg-a` / `pg-b` (playgrounds) behind `caddy-a` / `caddy-b` — use those names
with `docker compose logs`.

**Test knobs.** The e2e suite reads `AITP_FEDERATED_E2E` (the gate),
`ORG_A_URL` / `ORG_B_URL` (control-API URLs; defaults
`http://localhost:18000` / `:18001`) and `FEDERATED_AGENT_PORT` (default
`9100`). The agent port is pinned because the Caddy routes
(`Caddyfile.org-*`) and the Level 1 `PUBLIC_HOST` (`org-x.aitp.test:9100`) both
name it; the test passes it as `port` when it hosts each agent.

## No Docker? Same mechanism, one command

`tests/integration/test_federated_handshake.py` (gated on `AITP_E2E=1`) spawns
both agents at two real sockets on `127.0.0.1` in-process and drives the whole
resolve → handshake → invoke → fail-closed path. It opts out of the loopback
guard via `AITP_FEDERATION_ALLOW_LOOPBACK=1` (test-only); the Docker stacks keep
the guard on with real hostnames.

```bash
AITP_E2E=1 uv run pytest tests/integration/test_federated_handshake.py -v
```

## How it works (the plumbing)

- `/hosted-agents` (`src/aitp_playground/api/hosted.py`) is the federation
  primitive: it spawns a long-lived agent addressable at this service's
  **public origin**, resolves a peer `did:web` fail-closed, drives the
  handshake and makes the cross-origin call. The route list and status codes
  are in [docs/architecture.md § Hosted agents](https://github.com/agentidentitytrustprotocol/aitp-playground/blob/main/docs/architecture.md#hosted-agents-srcaitp_playgroundapihostedpy).
  `PUBLIC_HOST` / `PUBLIC_SCHEME` set the origin; the agent's manifest then
  advertises a real handshake endpoint instead of `localhost`
  (`hosting/bootstrap.py`).
- `AITP_DIDWEB_INSECURE_HOSTS=.aitp.test` lets Level 1 resolve `did:web` over
  http for the test hostnames only; production stays https-only.

Test-only assets — the CA/keys under `certs/` are throwaway and git-ignored.
Never reuse them anywhere real.
