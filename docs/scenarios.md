# Scenarios

A scenario is a declarative YAML that names the agents, the trust
discovery mode, and a sequence of workflow steps. The registry walks
`scenarios/` on startup and exposes each scenario by its ref:
`<pack>/<scenario>@<version>`.

## Layout on disk

```
scenarios/
├── _shared/
│   └── agents/                        # Agent manifests reused across scenarios
│       ├── researcher.yaml
│       ├── researcher-extended.yaml
│       ├── writer.yaml
│       └── analyzer.yaml
├── intra-org/
│   ├── pack.yaml                      # Pack metadata (slug, name, tags)
│   ├── research-and-write/
│   │   ├── 1.0.0/
│   │   │   ├── scenario.yaml
│   │   │   └── templates/             # Named variants — see "Template variants"
│   │   │       ├── trust-strict.yaml
│   │   │       └── revoking.yaml
│   │   └── 1.1.0/scenario.yaml
│   ├── scoped-capabilities/1.0.0/scenario.yaml
│   ├── delegation-chain/1.0.0/scenario.yaml
│   ├── revocation-demo/1.0.0/scenario.yaml
│   └── trust-gate/1.0.0/scenario.yaml
├── cross-cloud/
│   ├── pack.yaml
│   └── distributed-review/1.0.0/scenario.yaml
└── cross-org/
    ├── pack.yaml
    └── federated-analysis/1.0.0/scenario.yaml
```

Loader rules (`registry/loader.py`):
- Directories starting with `_` are ignored at the pack level — that's
  how `_shared/` is hidden from pack discovery.
- A pack needs `pack.yaml`; missing it is logged and the pack is
  skipped.
- Inside a pack, every `<scenario>/<version>/scenario.yaml` becomes one
  scenario version. The ref is built from the file's own metadata
  (`pack`, `scenario`, `version`), not from the path, so be consistent
  between path and metadata or the ref will surprise you.
- A `templates/` directory next to `scenario.yaml` holds named
  variant overrides (`kind: ScenarioTemplate`). Files without that
  `kind` are skipped with a warning — they're not loaded as scenarios
  or as templates.

`!include` is supported in any YAML loaded by the registry, resolved
relative to the including file. Use it to keep big scenarios readable
or to share input snippets.

## Pack file

```yaml
apiVersion: aitp.dev/v1
kind: ScenarioPack
metadata:
  slug: intra-org           # used in scenario refs as the prefix
  name: Intra-Org Scenarios
  description: Agents within the same organisation
  tags: [intra-org, same-trust-domain]
```

## Agent manifest

A manifest is reusable across scenarios. It declares the framework, how
to launch the worker, and what AITP capabilities the agent offers.

```yaml
apiVersion: aitp.dev/v1
kind: AgentManifest
metadata:
  id: researcher
  name: Research Analyst
  framework: crewai             # crewai | langchain | langgraph | custom
  version: 1.0.0
spec:
  entrypoint:
    type: python_module         # or python_file
    value: researcher.main
  host:
    python: python3             # optional override; falls back to AGENT_PYTHON
    cwd: agents/researcher      # relative to project root, or absolute
    startupTimeoutMs: 30000
    env:                        # optional, merged on top of inherited env
      MY_VAR: "value"
  aitp:
    offered_caps: [research.query]
    display_name: Research Analyst
    identity_type: pinned_key   # only value supported today
    ttl_secs: 3600
  did_web: false
```

Key behaviors:
- `entrypoint.type=python_module` becomes `python3 -m researcher.main`;
  `python_file` becomes `python3 path/to/file.py`. See `hosting/adapters/base.py`.
- `host.cwd` is resolved relative to the project root (parent of
  `scenarios_dir`). Absolute paths pass through.
- Adapters set `PYTHONPATH` to include `agents/base` (for the shared
  modules) and `agents/` (for `<package>.main` imports). Don't add
  these yourself.
- `aitp.offered_caps` is what shows up on the manifest the agent
  serves, and it's what the runner uses to route capability calls.
- `did_web` is informational on the manifest itself; the runtime
  decision to expose `/.well-known/did.json` comes from the scenario's
  per-agent `did_web_host` (see below).

## Scenario file

```yaml
apiVersion: aitp.dev/v1
kind: ScenarioVersion
metadata:
  pack: intra-org
  scenario: research-and-write
  version: 1.0.0
  name: Research and Write
  summary: >
    Two-sentence pitch shown in /scenarios listings.
  tags: [research, writing, crewai, langchain]

spec:
  inputs:
    schema:                       # JSON Schema; validated by ScenarioRunner
      type: object
      properties:
        topic:   { type: string, default: "AI agent collaboration" }
        style:   { type: string, enum: [academic, casual, technical], default: casual }
      required: [topic]

  agents:
    - id: researcher              # logical ID inside this scenario
      ref: _shared/agents/researcher  # path under scenarios_dir, no .yaml suffix
      port_offset: 0              # AGENT_BASE_PORT + offset
      org: internal               # internal | external (affects seed namespace)
      cloud: aws-us-east          # informational; never used by the runner
      did_web_host: localhost:8101 # set this to expose did:web for this agent

  trust:
    boundary: intra_org           # intra_org | cross_org | cross_cloud
    discovery: static             # static | did_web | cp_registry
    eager: true                   # run pairwise handshakes before workflow

  workflow:
    steps:
      - id: trust
        description: All agents perform AITP mutual handshake
      - id: research
        agent: researcher
        capability: research.query
        input_template: "{{ inputs.topic }}"
      - id: write
        agent: writer
        capability: write.content
        input_from: research
```

The Pydantic models are in `src/aitp_playground/registry/models.py`;
they are the canonical schema. Anything the YAML carries that isn't
defined there is ignored.

## Workflow steps

A step's `type` defaults sensibly when omitted:
- Has `agent` + `capability` → `workflow`.
- Otherwise → `meta` (skipped; useful for narration in the event log).

| `type` | Required fields | What it does |
| --- | --- | --- |
| `workflow` (default) | `agent`, `capability` | Routes to the agent that offers `capability`; if `agent` itself offers it, executes locally via `/admin/self-execute`. Otherwise calls cross-agent via `/admin/invoke`. |
| `handshake` | `initiator`, `responder`, optional `requested_grants` | Runs one direction of the AITP handshake. **Does not** auto-mirror. |
| `capability_call_no_trust` | `agent`, `target_agent`, `capability`, optional `expect_status` | POST directly to the target's `/capabilities/<name>` with no TCT — used to observe the 403. |
| `capability_probe` | `agent`, `target_agent`, `capability`, optional `expect_status` | Invoke via `/admin/invoke` and inspect the returned status; doesn't fail the run on a non-2xx. |
| `delegate` | `delegator`, `delegatee`, `via_peer`, `scope`, optional `ttl_secs` | `delegator` issues a `DelegationToken` to `delegatee` using the TCT it received from `via_peer`. |
| `redeem_delegation` | `delegatee`, `target`, `via_delegation` | `delegatee` POSTs the prior step's token to `target`'s `/aitp/delegation/redeem`; receives a fresh TCT bound to its own key. |
| `revoke_tct` | `issuer`, `audience`, optional `via_cp`, optional `reason` | Walks the event log to find the most recent TCT `issuer` granted to `audience`, then POSTs its jti to `issuer`'s `/admin/revoke-tct`. When `via_cp: true`, also POSTs the jti to the CP's `/api/revocation/entries` and asks the audience to pull the updated signed list from `/.well-known/aitp-revocation-list` — see `intra-org/revocation-via-cp`. |
| `rotate_keys` | `agent` | The named agent replaces its keypair, rebuilds its manifest under the new AID, and clears in-flight handshake sessions. Subsequent capability calls that present TCTs issued under the old AID are rejected by `verify_capability_tct`'s issuer-AID guard — see `intra-org/key-rotation`. |
| `enroll_with_cp` | `agent` | The named agent posts its current manifest to the Control Plane's `/api/registry/enroll` to mint a one-time bearer token, then re-posts to `/api/registry/agents` with that token to register. When `CP_BASE_URL` is unset the step skips. See `intra-org/external-enrollment`. |
| `cp_subscribe_webhook` | optional `events` | Register a webhook on the Control Plane whose URL points back at this run's `POST /webhooks/cp/{run_id}` receiver. CP returns the webhook id and a secret; the playground stores the secret on the run record so subsequent deliveries can be HMAC-verified (`X-Aitp-Signature: sha256=<hex>`). `events: []` (or omitted) subscribes to every deliverable CP type. When `CP_BASE_URL` is unset the step skips. See `intra-org/webhook-subscription`. |
| `meta` | — | No-op; records `step.skipped`. |

### Fault injection

Any `handshake`, `workflow`, or `capability_probe` step can carry a
`fault:` block. The runner mutates the call's target before issuing it
so the step exercises a failure path, and records the outcome as
`{fault_injected: true, kind, target, error}` in step_outputs without
raising the run — downstream steps can branch on the result.

```yaml
- id: doomed_call
  type: capability_probe
  agent: researcher
  target_agent: writer
  capability: write.content
  fault:
    kind: peer_offline
    note: "writer's port is rewritten to an unbound port"
```

Supported `fault.kind` values:

| Kind | What it does |
| --- | --- |
| `manifest_404` | Rewrites the targeted peer's manifest URL to a path that 404s. Use to demonstrate "peer is unreachable for discovery" on a handshake or delegate step. |
| `peer_offline` | Rewrites the targeted peer's port to a closed port. Use to demonstrate transport-level connection failures on handshake or workflow steps. |

See `intra-org/fault-injection` for a worked example. New events:
`step.fault_injected`, `step.fault_complete`.

### Input plumbing

Each workflow step gets exactly one input:
- `input_from: <prior_step_id>` — use that step's output verbatim. This
  is how data flows from `research` → `write` → `analyse`.
- `input_template: "..."` — string with `{{ inputs.<key> }}`
  placeholders. Only **flat** substitution against the scenario inputs.
- Neither → the full `inputs` dict.

### Capability routing

`ScenarioRunner._find_capability_holder` is the lookup:
1. If `step.agent` itself offers `step.capability`, use it (self-execute).
2. Otherwise pick the first agent in `scenario.spec.agents` that offers
   it.

`prefer=step.agent` exists to keep `agent: X, capability: Y` as a
self-execute when X actually offers Y — otherwise a scenario like
`delegation-chain` (where both `researcher` and `sub-researcher` offer
`research.query`) would silently route to the wrong agent. If you
write a scenario with multiple holders, name the one you want via
`agent:`.

### Trust eager flag

Eager handshakes (`spec.trust.eager: true`, the default) run a
bidirectional handshake between every pair of agents before any
workflow step. After this, every agent holds a TCT for every other
agent.

Set `eager: false` when the scenario itself demonstrates trust gating
or scoped grants — `trust-gate`, `scoped-capabilities`,
`revocation-demo`, and `delegation-chain` all do this. Those scenarios
control timing and scope themselves via explicit `handshake` steps.

## Template variants

A scenario version can ship named *templates* under
`<pack>/<scenario>/<version>/templates/<name>.yaml`. A template is a
named override on top of the base scenario — same `scenario_ref`,
different posture. Use templates when two demonstrations share
participants and inputs but diverge on trust posture or workflow shape.

```yaml
# scenarios/intra-org/research-and-write/1.0.0/templates/trust-strict.yaml
apiVersion: aitp.dev/v1
kind: ScenarioTemplate
metadata:
  name: trust-strict
  summary: probe a 403 before the explicit handshake
spec:
  trust:
    eager: false       # field-level patch — boundary/discovery fall through
  workflow:
    steps:             # full replacement of base workflow.steps
      - id: probe_no_tct
        type: capability_call_no_trust
        ...
```

Merge rules:

- `trust` patches the base field-by-field (e.g. flip `eager` without
  restating `boundary`).
- `agents` and `workflow.steps` are **full replacements**. Partial
  list patching is intentionally not supported — the merge stays
  deterministic and easy to read.
- Anything the template omits falls through unchanged.

The registry indexes templates and exposes them in three places:

- `GET /scenarios/<pack>/<scenario>@<version>` includes a
  `templates: [{name, summary}, ...]` list.
- `GET /scenarios/<pack>/<scenario>@<version>/templates` lists them.
- `GET /scenarios/<pack>/<scenario>@<version>/templates/<name>`
  returns the resolved (merged) scenario.

Run a templated scenario via `POST /runs`:

```json
{
  "scenario_ref": "intra-org/research-and-write@1.0.0",
  "template": "trust-strict",
  "inputs": {"topic": "AI agents"}
}
```

The CLI surfaces them too:

```bash
# Lists available templates at the bottom of base dry-run output
uv run python -m aitp_playground.cli dry-run intra-org/research-and-write@1.0.0 \
  --inputs '{"topic":"test"}'

# Merge a template and print the resolved plan
uv run python -m aitp_playground.cli dry-run intra-org/research-and-write@1.0.0 \
  --template trust-strict --inputs '{"topic":"test"}'
```

`lint` walks each template and applies the same checks as the base
scenario (agent / capability refs, step graph) against the merged
output.

## Validating

```bash
# Validate everything under scenarios_dir
uv run python -m aitp_playground.cli validate

# Validate a specific pack or scenario
uv run python -m aitp_playground.cli validate scenarios/intra-org
uv run python -m aitp_playground.cli validate scenarios/intra-org/research-and-write
```

The validator runs Pydantic over every scenario and agent manifest and
reports `ok` or `FAIL  <path>: <pydantic error>`. It does not exercise
any runtime behavior — for that, use:

```bash
uv run python -m aitp_playground.cli dry-run intra-org/research-and-write@1.0.0 \
  --inputs '{"topic":"test"}'
```

`dry-run` adds schema validation against the scenario's `inputs.schema`
and prints the agent/capability layout.

## Worked example — adding a scenario

Goal: a new intra-org scenario where the writer asks the researcher to
review what it wrote.

1. Pick a slot:
   `scenarios/intra-org/writer-asks-back/1.0.0/scenario.yaml`.
2. Reuse existing agent manifests — `researcher` and `writer` are in
   `_shared/agents/`.
3. Write the YAML:

   ```yaml
   apiVersion: aitp.dev/v1
   kind: ScenarioVersion
   metadata:
     pack: intra-org
     scenario: writer-asks-back
     version: 1.0.0
     name: Writer Asks Back
     summary: Writer drafts; researcher critiques the draft.

   spec:
     inputs:
       schema:
         type: object
         properties:
           topic: { type: string, default: "AI agent identity" }
         required: [topic]

     agents:
       - id: researcher
         ref: _shared/agents/researcher
         port_offset: 0
       - id: writer
         ref: _shared/agents/writer
         port_offset: 1

     trust:
       boundary: intra_org
       discovery: static

     workflow:
       steps:
         - id: research
           agent: researcher
           capability: research.query
           input_template: "{{ inputs.topic }}"
         - id: write
           agent: writer
           capability: write.content
           input_from: research
         - id: critique
           agent: researcher
           capability: research.query
           input_from: write
   ```

4. `dry-run` it, then `POST /runs` and check the event log.

If you need a *new* capability, you'll also touch an agent worker —
see [agents.md](agents.md).

## Scenarios in the box

| Ref | What it shows |
| --- | --- |
| `intra-org/research-and-write@1.0.0` | The simplest happy path. |
| `intra-org/research-and-write@1.1.0` | Same + an analyzer at the end. |
| `intra-org/trust-gate@1.0.0` | A capability call with no TCT is rejected; then it succeeds after handshake. |
| `intra-org/scoped-capabilities@1.0.0` | Grant intersection — TCT scoped to one of two offered caps. |
| `intra-org/revocation-demo@1.0.0` | RFC-AITP-0008: revoke a TCT's jti; subsequent calls 403. |
| `intra-org/revocation-via-cp@1.0.0` | RFC-AITP-0008 federation: revocation propagates through the CP's signed `/.well-known/aitp-revocation-list`. |
| `intra-org/delegation-chain@1.0.0` | RFC-AITP-0006: single-hop delegation + redeem. |
| `intra-org/delegation-multihop@1.0.0` | RFC-AITP-0011: two-hop chain (researcher → sub-researcher → analyst). |
| `intra-org/key-rotation@1.0.0` | RFC-AITP-0007: writer rotates keys; pre-rotation TCTs become invalid. |
| `intra-org/fault-injection@1.0.0` | Operator-injected `manifest_404` and `peer_offline` faults; run continues with structured failure outcomes. |
| `intra-org/external-enrollment@1.0.0` | Agent self-enrolls via `POST /api/registry/enroll` then `POST /api/registry/agents` with the issued bearer token. |
| `intra-org/webhook-subscription@1.0.0` | Playground registers a CP webhook and CP fans `handshake.complete` / other audit events back via `POST /webhooks/cp/{run_id}`; inspect deliveries with `GET /runs/{id}/cp-deliveries`. |
| `cross-cloud/distributed-review@1.0.0` | Three agents; did:web discovery. |
| `cross-org/federated-analysis@1.0.0` | CP-registry discovery for an external agent (falls back to static). |

Each is a useful template — copy the closest one and edit.
