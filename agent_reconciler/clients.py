"""Two clients: the mgmt cluster (in-cluster SA) and the KubeVirt cluster (kubeconfig).

Everything here is READ-ONLY in v0. No create/patch/delete calls exist yet — that is
deliberate: v0 physically cannot mutate either cluster.
"""
import logging

from kubernetes import client, config
from kubernetes.client.rest import ApiException

log = logging.getLogger("agent-reconciler")

AGENT_GROUP = "agent-install.openshift.io"
AGENT_VERSION = "v1beta1"
KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"


def mgmt_api() -> client.CustomObjectsApi:
    """CRD client for the management (HyperShift/MCE) cluster we run inside.

    Loads into its OWN Configuration rather than the process-wide default singleton.
    Bare `load_incluster_config()` calls `Configuration.set_default()`, making this
    client's credentials shared mutable global state that any other library in the
    process can read or overwrite. Kopf has its own explicit credentials now
    (see auth.py), so nothing needs the default -- keep it untouched.
    """
    cfg = client.Configuration()
    config.load_incluster_config(client_configuration=cfg)
    return client.CustomObjectsApi(client.ApiClient(cfg))


def kubevirt_api(kubeconfig_path: str) -> client.CustomObjectsApi:
    """CRD client for the external KubeVirt cluster, from a mounted kubeconfig."""
    cfg = client.Configuration()
    config.load_kube_config(config_file=kubeconfig_path, client_configuration=cfg)
    return client.CustomObjectsApi(client.ApiClient(cfg))


def assert_authenticated(api: client.CustomObjectsApi, which: str) -> None:
    """Make one real call and confirm we are not talking as system:anonymous.

    Catches the failure mode where credentials LOAD fine but are never SENT.
    Without this the only symptom is an opaque retrying 'forbidden ... /apis' loop.

    SCOPE -- read this before trusting it: this only proves the plain `kubernetes`
    clients built in this module. It does NOT cover Kopf, which maintains its own
    separate aiohttp session built from the @kopf.on.login() handler. Those two can
    and did disagree: the version bug in auth.py broke Kopf's connection while these
    clients kept working, so this assertion passed while the operator was dead.
    operator.startup() asserts the Kopf side separately.
    """
    try:
        client.VersionApi(api.api_client).get_code()
    except ApiException as e:
        if e.status in (401, 403) and "anonymous" in str(e.body or ""):
            raise RuntimeError(
                f"{which} cluster rejected our credentials as system:anonymous -- the token "
                f"was loaded but not sent. `system:anonymous` names NO user, so this is a "
                f"CLIENT problem, not RBAC (an RBAC denial would name the ServiceAccount). "
                f"Check that the token is mounted and non-empty."
            ) from e
        raise
    log.warning("%s cluster: authenticated OK", which)


def list_agents(api: client.CustomObjectsApi) -> list:
    return api.list_cluster_custom_object(
        AGENT_GROUP, AGENT_VERSION, "agents"
    ).get("items", [])


def list_vms(api: client.CustomObjectsApi, label_selector: str | None = None) -> list:
    return api.list_cluster_custom_object(
        KUBEVIRT_GROUP, KUBEVIRT_VERSION, "virtualmachines",
        label_selector=label_selector,
    ).get("items", [])


def list_vmis(api: client.CustomObjectsApi) -> list:
    return api.list_cluster_custom_object(
        KUBEVIRT_GROUP, KUBEVIRT_VERSION, "virtualmachineinstances"
    ).get("items", [])
