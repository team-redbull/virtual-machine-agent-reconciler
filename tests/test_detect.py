"""Prove the eyes. Run: pytest -q"""
from agent_reconciler import detect

LABEL = "infra.example.com/vm-id"


INFRAENV_LABEL = "infraenvs.agent-install.openshift.io"


def _agent(vm_id=None, cluster=None, inventory=None):
    a = {"metadata": {"labels": {}}, "spec": {}}
    if vm_id:
        a["metadata"]["labels"][LABEL] = vm_id
    if inventory:
        a["metadata"]["labels"][INFRAENV_LABEL] = inventory
    if cluster:
        a["spec"]["clusterDeploymentName"] = {"namespace": cluster[0], "name": cluster[1]}
    return a


def test_inventory_filter_matches_only_vm_backed():
    vm = _agent(inventory="virtual-machine-16cpu32gi")
    physical = _agent(inventory="bare-metal-pool")
    unlabeled = _agent()
    assert detect.is_managed_inventory(vm, INFRAENV_LABEL, "virtual-machine") is True
    assert detect.is_managed_inventory(physical, INFRAENV_LABEL, "virtual-machine") is False
    assert detect.is_managed_inventory(unlabeled, INFRAENV_LABEL, "virtual-machine") is False


def _vmi(vm_id, node):
    return {"metadata": {"labels": {LABEL: vm_id}}, "status": {"nodeName": node}}


def test_unclaimed_and_unlabeled_agents_are_ignored():
    agents = [_agent(), _agent(vm_id="x"), _agent(cluster=("ns", "c"))]
    assert detect.cluster_node_skew(agents, {}, LABEL) == {}


def test_skew_counts_same_cluster_per_node():
    cluster = ("ns", "test")
    agents = [_agent("a", cluster), _agent("b", cluster), _agent("c", cluster)]
    vmis = [_vmi("a", "node-A"), _vmi("b", "node-A"), _vmi("c", "node-B")]
    nodes = detect.vmi_nodes_by_vm_id(vmis, LABEL)
    skew = detect.cluster_node_skew(agents, nodes, LABEL)
    assert skew[cluster]["node-A"] == 2
    assert skew[cluster]["node-B"] == 1


def test_stopped_vm_has_no_node_and_is_skipped():
    cluster = ("ns", "test")
    agents = [_agent("a", cluster)]
    # VMI exists but not scheduled yet (no nodeName)
    vmis = [{"metadata": {"labels": {LABEL: "a"}}, "status": {}}]
    nodes = detect.vmi_nodes_by_vm_id(vmis, LABEL)
    assert nodes == {}
    assert detect.cluster_node_skew(agents, nodes, LABEL) == {}
