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

## Roadmap

- **v1** cleanup — delete Agent + BMH on confirmed orphan, behind `--dry-run`. Needs
  `rbac.allowWrite=true` in the chart. Adds the VM finalizer fast-path.
- **v2** label sync — maintain the `target-cluster` label on VMI + pod.
- **v3** rebalance — issue `VirtualMachineInstanceMigration` for skew, with a per-VM cooldown.
