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

from . import auth, clients, config, detect

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


def _assert_kopf_login_is_explicit() -> None:
    """Fail fast if Kopf would fall back to its built-in login instead of ours.

    This is the guard that was missing when the operator sat in a `system:anonymous`
    retry loop: clients.assert_authenticated() only ever probed the plain `kubernetes`
    clients (which were fine), never Kopf's own connection (which was not). If the
    @kopf.on.login() handler below is ever removed or fails to register, Kopf silently
    reverts to login_via_client -- so assert that it did not, at startup, loudly.

    Best-effort: Kopf's registry internals are private, so a lookup failure only warns.
    """
    try:
        from kopf._core.intents import causes
        handlers = kopf.get_default_registry()._activities.get_all_handlers()
        logins = [h for h in handlers
                  if h.activity == causes.Activity.AUTHENTICATION]
        explicit = [h for h in logins if not getattr(h, "_fallback", False)]
    except Exception as e:  # pragma: no cover - private API moved
        log.warning("could not introspect kopf login handlers (%s) -- skipping check", e)
        return

    if not explicit:
        raise RuntimeError(
            "No explicit @kopf.on.login() handler is registered, so kopf will fall back "
            "to %s. On kopf<=1.44.5 with kubernetes>=36.0.2 that fallback yields token=None "
            "and every API call authenticates as system:anonymous. Restore the login() "
            "handler in agent_reconciler/operator.py." % ([h.id for h in logins] or "nothing")
        )
    log.warning("kopf login handler(s) in use: %s", [str(h.id) for h in explicit])


@kopf.on.login()
def login(settings: kopf.OperatorSettings = None, **_) -> kopf.ConnectionInfo:
    """Kopf's credentials for the MANAGEMENT cluster, read straight from the SA mount.

    Registering any @kopf.on.login() handler makes Kopf skip ALL of its built-in
    fallbacks (login_via_pykube / login_via_client / ...), which are registered with
    _fallback=True. That is the point: login_via_client re-reads the shared default
    `kubernetes.client.Configuration` singleton and, on kopf<=1.44.5 with
    kubernetes>=36.0.2, looks up the bearer token under the wrong api_key entry and
    silently yields token=None -> every request goes out as system:anonymous.
    See agent_reconciler/auth.py for the full write-up.

    `settings` defaults to None so this can be called by hand for diagnosis:
        kubectl exec deploy/agent-reconciler -- \
            python -c "from agent_reconciler.operator import login; print(login())"
    """
    trust_env = bool(settings.networking.trust_env) if settings is not None else False
    conn = auth.service_account_connection(trust_env=trust_env)
    log.warning("kopf will authenticate to %s as the mounted ServiceAccount "
                "(scheme=%s, token=%d chars)", conn.server, conn.scheme, len(conn.token))
    return conn


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, **_):
    # Keep v0 quiet on the K8s event stream — it's a detector, not an actor.
    settings.posting.level = logging.WARNING

    # Kopf's connection is separate from the two clients below; check it separately.
    _assert_kopf_login_is_explicit()

    _state["mgmt"] = clients.mgmt_api()
    _state["kv"] = clients.kubevirt_api(config.KUBEVIRT_KUBECONFIG)

    # Fail loudly on the classic "system:anonymous" setup, instead of letting
    # Kopf retry a forbidden /apis call nine times with an opaque error.
    clients.assert_authenticated(_state["mgmt"], "management")
    clients.assert_authenticated(_state["kv"], "kubevirt")

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
