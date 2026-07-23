# agent-reconciler — v0 (read-only detector)

Cross-cluster detector for the HyperShift/MCE ↔ KubeVirt platform. Runs on the
**management** cluster and reports two things **without touching anything**:

- **Orphan candidates** — Agents whose backing VM is *confirmed absent* in KubeVirt.
- **Cluster/node skew** — a target cluster whose VMIs are clumped on one infra node.

v0 has no delete or migrate code paths at all. It logs what v1/v3 *would* do, so you
can validate detection against real clusters before it ever gets hands.

> **Deployment lives in a separate repo:** `agent-reconciler-chart` (Helm).
> This repo builds the image; that repo installs it.

## Prerequisite (do this first)

Your GitOps must stamp the **same `vm-id` label on both sides**:

- on the **VM** in the KubeVirt cluster, and
- on the **Agent** in the mgmt cluster.

Set `VM_ID_LABEL` to that key. Without it the detector has nothing to join on and
every check is a silent no-op.

## Layout

```
agent_reconciler/
  operator.py   # Kopf entrypoint: orphan timer + skew sweep (log-only)
  detect.py     # pure logic, no I/O — the part worth unit-testing
  clients.py    # mgmt (in-cluster) + KubeVirt (kubeconfig) clients, read-only
  config.py     # every knob, env-driven
tests/          # unit tests for detect.py
Dockerfile
```

## Test

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -q
```

## Run locally

```bash
export VM_ID_LABEL="infra.example.com/vm-id"
export INVENTORY_NAME_FILTER="virtual-machine"
export KUBEVIRT_KUBECONFIG="$HOME/.kube/kubevirt-config"
# mgmt cluster comes from your current kubectl context
kopf run -m agent_reconciler.operator --all-namespaces
```

Watch for `ORPHAN CANDIDATE` and `SKEW` lines.

## Build & publish

```bash
podman build -t REGISTRY/agent-reconciler:v0 .
podman push REGISTRY/agent-reconciler:v0
```

Then bump `image.tag` in the chart repo. **The image tag you push here is the
contract between the two repos** — the chart does not build anything.

## Environment knobs

| Env | Default | Meaning |
|---|---|---|
| `VM_ID_LABEL` | `infra.example.com/vm-id` | shared join key on VM + Agent |
| `INVENTORY_NAME_FILTER` | `virtual-machine` | only manage inventories whose name contains this |
| `INFRAENV_LABEL` | `infraenvs.agent-install.openshift.io` | Agent label holding the inventory name |
| `MISS_THRESHOLD` | `3` | consecutive confirmed-absent checks before flagging |
| `MAX_SKEW` | `1` | max VMIs of one cluster on a node before flagging |
| `CHECK_INTERVAL` | `60` | per-Agent orphan check (s) |
| `SKEW_INTERVAL` | `120` | global skew sweep (s) |
| `METRICS_PORT` | `8080` | Prometheus metrics port |

## Safety rule baked into the code

**"Can't reach the VM" ≠ "VM is gone."** A failed or timed-out KubeVirt query
**skips** the cycle and does **not** advance the miss counter. Only a successful
query returning zero matches counts as absence, and only `MISS_THRESHOLD` in a row
flags an orphan. This is the single highest-risk piece of logic here — if you change
`check_orphan`, preserve this.

The inventory filter exists for the same reason: hosts in a *physical* inventory have
no VM in KubeVirt, so without the filter they would all read as "confirmed absent"
and be flagged as orphans.

## Troubleshooting

Three failure modes hit during first deployment, all fixed in this repo. Kept here
because each produces a misleading error.

### `ModuleNotFoundError: No module named 'agent_reconciler'`

Installed console scripts (`kopf` lives in `/usr/local/bin`) do **not** put the
working directory on `sys.path`, so `WORKDIR /app` alone is not enough. Fixed by
`ENV PYTHONPATH=/app` in the Dockerfile. Verify before pushing:

```bash
podman run --rm --entrypoint python REGISTRY/agent-reconciler:v0 \
  -c "import agent_reconciler.operator; print('import OK')"
```

### `No usable temporary directory found in ['/tmp', ...]`

The chart sets `readOnlyRootFilesystem: true`, which makes `/tmp` read-only too;
Python needs a writable temp dir at import time. Fixed in the chart by mounting an
`emptyDir` at `/tmp` — the hardening stays on.

### `forbidden: User "system:anonymous" cannot get path "/apis"`

**Not an RBAC problem, and not the KubeVirt kubeconfig.** kubernetes-client
>=36.0.2 moved the bearer token from `api_key['authorization']` to
`api_key['BearerToken']`. Older kopf releases only read the old key, extract an
empty token, and send every request unauthenticated.

Tell-tale signs: `login_via_client` reports **success** (it builds a valid config
object, just with `token=None`), and the *first real* call fails as anonymous.

Fixed by the version pins in `requirements.txt`. Confirm what actually got
installed — a cached layer can silently keep an old kopf:

```bash
podman build --no-cache -t REGISTRY/agent-reconciler:v0 .
podman run --rm --entrypoint pip REGISTRY/agent-reconciler:v0 list | grep -Ei 'kopf|kubernetes'
```

> Note: do **not** debug this with `Configuration().api_key.get('authorization')` —
> that key is empty by design on kubernetes >=36.0.2 even when auth is healthy.
> Check `api_key.get('BearerToken')` instead.

Read the *identity* in any authorization error before touching RBAC:
`system:anonymous` means authentication never happened, so granting permissions
cannot help. A real RBAC failure names your ServiceAccount.

## Roadmap

- **v1** cleanup — delete Agent + BMH on confirmed orphan, behind `--dry-run`. Needs
  `rbac.allowWrite=true` in the chart. Adds the VM finalizer fast-path.
- **v2** label sync — maintain the `target-cluster` label on VMI + pod.
- **v3** rebalance — issue `VirtualMachineInstanceMigration` for skew, with a per-VM cooldown.
