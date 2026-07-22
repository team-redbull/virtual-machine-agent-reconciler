# agent-reconciler — v0 (read-only detector)

Cross-cluster detector for the HyperShift/MCE ↔ KubeVirt platform. It runs on the
**management** cluster and reports two things **without touching anything**:

- **Orphan candidates** — Agents whose backing VM is *confirmed absent* in KubeVirt.
- **Cluster/node skew** — a target cluster whose VMIs are clumped on one infra node.

v0 has no delete or migrate code paths at all. It logs what v1/v3 *would* do, so you
can validate the detection against real clusters before it ever gets hands.

## Prerequisite (do this first)

Your GitOps must stamp the **same `vm-id` label on both sides**:
- on the **VM** in the KubeVirt cluster, and
- on the **Agent** in the mgmt cluster.

Set `VM_ID_LABEL` to that key. Without it the detector has nothing to join on.

## Test the logic

```bash
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -q
```

## Run locally (against your kubeconfigs)

```bash
export VM_ID_LABEL="infra.example.com/vm-id"
export KUBEVIRT_KUBECONFIG="$HOME/.kube/kubevirt-config"
# uses your current context for the mgmt cluster
kopf run -m agent_reconciler.operator --all-namespaces
```

Watch for `ORPHAN CANDIDATE` and `SKEW` lines. Metrics at `:8080`:
`reconciler_orphan_candidates`, `reconciler_cluster_node_skew{cluster=...}`.

## Deploy (glue, via your GitOps)

1. Create the KubeVirt kubeconfig secret (read access to VMs + VMIs on that cluster):
   `kubectl -n agent-reconciler create secret generic kubevirt-kubeconfig --from-file=kubeconfig=...`
2. `kubectl create ns agent-reconciler`
3. Apply `deploy/rbac.yaml` and `deploy/deployment.yaml` (set image + `VM_ID_LABEL`).

## Knobs

| Env | Default | Meaning |
|---|---|---|
| `VM_ID_LABEL` | `infra.example.com/vm-id` | shared join key on VM + Agent |
| `INVENTORY_NAME_FILTER` | `virtual-machine` | only manage inventories whose name contains this substring |
| `INFRAENV_LABEL` | `infraenvs.agent-install.openshift.io` | Agent label holding the inventory (InfraEnv) name |
| `MISS_THRESHOLD` | `3` | consecutive confirmed-absent checks before flagging |
| `MAX_SKEW` | `1` | max VMIs of one cluster on a node before flagging |
| `CHECK_INTERVAL` | `60` | per-Agent orphan check (s) |
| `SKEW_INTERVAL` | `120` | global skew sweep (s) |

## Safety rule baked in

"Can't reach the VM" ≠ "VM is gone." A failed/timed-out KubeVirt query **skips** the
cycle and does **not** advance the miss counter. Only a successful query returning zero
matches counts as absence — and only `MISS_THRESHOLD` in a row flags an orphan.

## Next

- **v1** cleanup: delete Agent + BMH on confirmed orphan, behind `--dry-run`. Adds delete RBAC + the VM finalizer fast-path.
- **v2** label sync: maintain the `target-cluster` label on VMI + pod.
- **v3** rebalance: issue `VirtualMachineInstanceMigration` for skew, with a per-VM cooldown.
