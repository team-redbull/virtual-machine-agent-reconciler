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


def agent_hostname(agent: dict) -> str | None:
    """The human-readable hostname of an Agent, or None if not discovered yet.

    An Agent's `metadata.name` is the agent UUID minted by the discovery ISO
    (e.g. `2f8c1a34-...`), which is useless when reading logs or grepping for a
    machine. The readable name lives in two places, checked in priority order:

      1. `spec.hostname`           -- the operator-requested override, if set.
      2. `status.inventory.hostname` -- what the host actually reported.

    spec wins because that is what the host will be renamed TO; status is what
    it is called right now. Before inventory arrives both are absent, so callers
    must handle None (see `agent_ident`).
    """
    spec = agent.get("spec") or {}
    requested = spec.get("hostname")
    if requested:
        return str(requested)

    inventory = (agent.get("status") or {}).get("inventory") or {}
    reported = inventory.get("hostname")
    return str(reported) if reported else None


def agent_state(agent: dict) -> str | None:
    """Assisted-installer state (`status.debugInfo.state`), e.g. 'installed'."""
    state = ((agent.get("status") or {}).get("debugInfo") or {}).get("state")
    return str(state) if state else None


def agent_ident(agent: dict, vm_id_label: str) -> str:
    """One greppable identity string naming every object this Agent touches.

    Rendered as `key=value` pairs so log lines stay parseable:

        Agent host=worker-01.lab hostname=... uid=2f8c1a34-... ns=mce vm-id=vm-042 cluster=ns/prod

    `uid` is always present (it is metadata.name, the join key for kubectl);
    `host` is omitted rather than faked when inventory has not arrived yet, so
    an absent hostname is visibly absent instead of silently reading as a UUID.
    """
    meta = agent.get("metadata") or {}
    parts = ["Agent"]

    host = agent_hostname(agent)
    parts.append(f"host={host}" if host else "host=<no-inventory-yet>")

    parts.append(f"uid={meta.get('name')}")
    parts.append(f"ns={meta.get('namespace')}")

    vid = agent_vm_id(agent, vm_id_label)
    parts.append(f"vm-id={vid}" if vid else "vm-id=<UNLABELLED>")

    cluster = agent_cluster(agent)
    if cluster:
        parts.append(f"cluster={cluster[0]}/{cluster[1]}")
    else:
        parts.append("cluster=<unbound>")

    state = agent_state(agent)
    if state:
        parts.append(f"state={state}")

    return " ".join(parts)


def vm_ident(vm: dict) -> str:
    """Identity of a KubeVirt VirtualMachine / VirtualMachineInstance."""
    meta = vm.get("metadata") or {}
    node = (vm.get("status") or {}).get("nodeName")
    out = f"{meta.get('namespace')}/{meta.get('name')}"
    return f"{out} node={node}" if node else out


def agents_on_node(agents: list, vmi_nodes: dict[str, str], label: str,
                   cluster: tuple[str, str], node: str) -> list[dict]:
    """The Agents of `cluster` whose VMI is currently running on `node`.

    `cluster_node_skew` answers "how many", which is what the metric needs. This
    answers "which ones", which is what a human reading the SKEW line needs in
    order to act -- and is the list v3 would hand to the migration call.
    """
    out = []
    for a in agents:
        vid = agent_vm_id(a, label)
        # An unlabelled Agent has no VM to be "on" a node. Guard explicitly: a
        # `vmi_nodes.get(vid or "")` shortcut would match a stray ""-keyed entry
        # and silently attribute an unrelated host to this node.
        if vid is None or agent_cluster(a) != cluster:
            continue
        if vmi_nodes.get(vid) == node:
            out.append(a)
    return out


def agent_display_name(agent: dict) -> str:
    """Hostname if known, else the UUID. For compact list output."""
    return agent_hostname(agent) or str((agent.get("metadata") or {}).get("name"))
