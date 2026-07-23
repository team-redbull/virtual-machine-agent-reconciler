"""Pure functions — no cluster I/O — so the logic that will later drive deletions
and migrations can be unit-tested in isolation. This is the 'eyes' we prove first.
"""
from collections import Counter, defaultdict


def agent_vm_id(agent: dict, label: str) -> str | None:
    """The shared join key stamped on the Agent, or None if unlabeled."""
    return ((agent.get("metadata", {}) or {}).get("labels", {}) or {}).get(label)


def agent_inventory(agent: dict, infraenv_label: str) -> str | None:
    """The inventory (InfraEnv) name this Agent belongs to, or None."""
    return ((agent.get("metadata", {}) or {}).get("labels", {}) or {}).get(infraenv_label)


def is_managed_inventory(agent: dict, infraenv_label: str, name_filter: str) -> bool:
    """True only if the Agent's inventory name contains `name_filter`.

    Scopes the controller to VM-backed inventories. A physical inventory's hosts
    have no VM in the KubeVirt cluster, so without this they'd all read as
    'confirmed absent' and be wrongly flagged as orphans.
    """
    inv = agent_inventory(agent, infraenv_label)
    return inv is not None and name_filter in inv


def agent_cluster(agent: dict) -> tuple[str, str] | None:
    """(namespace, name) of the ClusterDeployment this Agent is bound to, or None
    if the Agent is still unclaimed (sitting in the warm pool)."""
    ref = (agent.get("spec", {}) or {}).get("clusterDeploymentName")
    if not ref:
        return None
    return (ref.get("namespace"), ref.get("name"))


def vmi_nodes_by_vm_id(vmis: list, label: str) -> dict[str, str]:
    """Map vm-id -> infra node, for every running VMI that carries the join key."""
    out: dict[str, str] = {}
    for vmi in vmis:
        vid = ((vmi.get("metadata", {}) or {}).get("labels", {}) or {}).get(label)
        node = (vmi.get("status", {}) or {}).get("nodeName")
        if vid and node:
            out[vid] = node
    return out


def cluster_node_skew(agents: list, vmi_nodes: dict[str, str], label: str
                      ) -> dict[tuple[str, str], Counter]:
    """For each bound cluster, count how many of its VMIs sit on each node.

    Returns {cluster: Counter(node -> count)}. Unclaimed or unlabeled agents,
    and agents whose VMI isn't running, are ignored.
    """
    by_cluster: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for a in agents:
        cluster = agent_cluster(a)
        vid = agent_vm_id(a, label)
        if cluster is None or vid is None:
            continue
        node = vmi_nodes.get(vid)
        if node:
            by_cluster[cluster][node] += 1
    return by_cluster
