"""Two clients: the mgmt cluster (in-cluster SA) and the KubeVirt cluster (kubeconfig).

Everything here is READ-ONLY in v0. No create/patch/delete calls exist yet — that is
deliberate: v0 physically cannot mutate either cluster.
"""
from kubernetes import client, config

AGENT_GROUP = "agent-install.openshift.io"
AGENT_VERSION = "v1beta1"
KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"


def mgmt_api() -> client.CustomObjectsApi:
    """CRD client for the management (HyperShift/MCE) cluster we run inside."""
    config.load_incluster_config()
    return client.CustomObjectsApi()


def kubevirt_api(kubeconfig_path: str) -> client.CustomObjectsApi:
    """CRD client for the external KubeVirt cluster, from a mounted kubeconfig."""
    cfg = client.Configuration()
    config.load_kube_config(config_file=kubeconfig_path, client_configuration=cfg)
    return client.CustomObjectsApi(client.ApiClient(cfg))


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
