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
  identity (`did:web:org-b.aitp.test`) and serving its own `did.json`,
  manifest, handshake, and capability endpoints there.
- org-A hosts the **researcher**, resolves the analyzer's DID **across the
  boundary**, and runs a real AITP handshake → TCT → capability call.
- **Fail-closed:** if a DID resolves back to `localhost`/`127.0.0.1`, the
  handshake is refused (HTTP 409). A green run can't be a disguised
  same-process handshake.

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
./federated/gen-ca.sh                       # one-time: mint CA + server certs
docker compose -f federated/docker-compose.federated-tls.yml up --build -d
AITP_FEDERATED_E2E=1 uv run pytest tests/e2e_federated/ -v
docker compose -f federated/docker-compose.federated-tls.yml down
```

The e2e suite is identical for both levels — only the compose file changes.

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

- `POST /hosted-agents` (`src/aitp_playground/api/hosted.py`) spawns one
  long-lived agent addressable at this service's **public origin** — the
  federation primitive. `PUBLIC_HOST` / `PUBLIC_SCHEME` set that origin; the
  agent's manifest then advertises a real handshake endpoint instead of
  `localhost` (`hosting/bootstrap.py`).
- `POST /hosted-agents/{id}/resolve-and-handshake` resolves a peer `did:web`
  (`trust/resolver.py`) **fail-closed** and drives the agent's own SDK
  handshake. `/invoke` makes the cross-origin capability call.
- `AITP_DIDWEB_INSECURE_HOSTS=.aitp.test` lets Level 1 resolve `did:web` over
  http for the test hostnames only; production stays https-only.

Test-only assets — the CA/keys under `certs/` are throwaway and git-ignored.
Never reuse them anywhere real.
