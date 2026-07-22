"""agent-reconciler v0 — READ-ONLY DETECTOR.

This process cannot mutate either cluster. It logs the actions v1/v3 *would* take:
  * ORPHAN CANDIDATE  -> v1 would delete the Agent + BMH
  * SKEW              -> v3 would live-migrate the excess VMIs

Run it against real clusters and watch the logs/metrics for a day before giving it hands.
"""
import asyncio
import logging

import kopf
from prometheus_client import Gauge, start_http_server

from . import clients, config, detect

log = logging.getLogger("agent-reconciler")

# Single-replica in v0: this in-memory miss counter is fine. Add a Lease-based
# leader election before scaling to >1 replica.
_state: dict = {"mgmt": None, "kv": None}
_miss: dict[tuple[str, str], int] = {}  # (namespace, name) -> consecutive confirmed-absent

ORPHAN_CANDIDATES = Gauge(
    "reconciler_orphan_candidates",
    "Agents whose backing VM is confirmed absent past the miss threshold",
)
CLUSTER_SKEW = Gauge(
    "reconciler_cluster_node_skew",
    "Max VMIs of one cluster landing on a single infra node",
    ["cluster"],
)


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_):
    # Keep v0 quiet on the K8s event stream — it's a detector, not an actor.
    settings.posting.level = logging.WARNING
    _state["mgmt"] = clients.mgmt_api()
    _state["kv"] = clients.kubevirt_api(config.KUBEVIRT_KUBECONFIG)
    start_http_server(config.METRICS_PORT)
    log.warning(
        "agent-reconciler v0 started (LOG ONLY — no writes). vm-id label=%s, "
        "inventory filter=%r on label %s, miss threshold=%d, max skew=%d",
        config.VM_ID_LABEL, config.INVENTORY_NAME_FILTER, config.INFRAENV_LABEL,
        config.MISS_THRESHOLD, config.MAX_SKEW,
    )
    asyncio.create_task(_skew_loop())


def _vm_inventory_filter(meta, **_) -> bool:
    """Kopf filter: only manage Agents whose inventory name matches the filter."""
    inv = (meta.get("labels") or {}).get(config.INFRAENV_LABEL)
    return bool(inv) and config.INVENTORY_NAME_FILTER in inv


@kopf.timer("agent-install.openshift.io", "v1beta1", "agents",
            interval=config.CHECK_INTERVAL, sharp=True, when=_vm_inventory_filter)
def check_orphan(name: str, namespace: str, meta: dict, logger, **_):
    """Per-Agent: is this Agent's VM confirmed absent in KubeVirt?"""
    label = config.VM_ID_LABEL
    key = (namespace, name)
    vid = (meta.get("labels") or {}).get(label)

    if not vid:
        logger.warning("agent %s/%s has no %s label — cannot map to a VM", namespace, name, label)
        return

    try:
        vms = clients.list_vms(_state["kv"], label_selector=f"{label}={vid}")
    except Exception as e:
        # CRITICAL SAFETY RULE: unreachable/error != absent. Skip, do NOT count.
        logger.warning("kubevirt query failed for %s/%s (vm-id=%s): %s — skipping, not counting",
                       namespace, name, vid, e)
        return

    if vms:
        _miss.pop(key, None)  # VM present — reset any streak
        return

    # Confirmed absent: the API answered and returned zero matches.
    _miss[key] = _miss.get(key, 0) + 1
    n = _miss[key]
    if n >= config.MISS_THRESHOLD:
        logger.error("ORPHAN CANDIDATE: agent %s/%s vm-id=%s absent for %d consecutive checks "
                     "(v1 would delete Agent + BMH here)", namespace, name, vid, n)
    else:
        logger.warning("agent %s/%s vm-id=%s absent (%d/%d) — below threshold, waiting",
                       namespace, name, vid, n, config.MISS_THRESHOLD)

    ORPHAN_CANDIDATES.set(sum(1 for c in _miss.values() if c >= config.MISS_THRESHOLD))


async def _skew_loop():
    while True:
        try:
            await asyncio.to_thread(_skew_pass)
        except Exception as e:
            log.warning("skew sweep failed: %s — skipping this pass", e)
        await asyncio.sleep(config.SKEW_INTERVAL)


def _skew_pass():
    """Global: group bound agents by cluster, count their VMIs per node."""
    label = config.VM_ID_LABEL
    agents = [a for a in clients.list_agents(_state["mgmt"])
              if detect.is_managed_inventory(a, config.INFRAENV_LABEL,
                                             config.INVENTORY_NAME_FILTER)]
    vmis = clients.list_vmis(_state["kv"])
    vmi_nodes = detect.vmi_nodes_by_vm_id(vmis, label)

    for cluster, node_counts in detect.cluster_node_skew(agents, vmi_nodes, label).items():
        cname = f"{cluster[0]}/{cluster[1]}"
        node, count = node_counts.most_common(1)[0]
        CLUSTER_SKEW.labels(cluster=cname).set(count)
        if count > config.MAX_SKEW:
            log.error("SKEW: cluster %s has %d VMIs on node %s (max=%d) — "
                      "v3 would migrate the excess", cname, count, node, config.MAX_SKEW)
