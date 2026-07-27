"""Every log line must name the Agent in a way a human can act on.

An Agent's metadata.name is the discovery-ISO UUID. Logs keyed only on that are
unusable: you cannot tell which physical machine is being flagged as an orphan
without a second lookup. These tests pin the readable identity into the output.
"""
import logging

import pytest

from agent_reconciler import config, detect, operator


def _agent(uid="2f8c1a34-0000-4000-8000-000000000001", ns="mce", spec_host=None,
           inv_host=None, vm_id=None, cluster=None, state=None):
    a = {"metadata": {"name": uid, "namespace": ns, "labels": {}},
         "spec": {}, "status": {}}
    if spec_host:
        a["spec"]["hostname"] = spec_host
    if inv_host:
        a["status"]["inventory"] = {"hostname": inv_host}
    if vm_id:
        a["metadata"]["labels"][config.VM_ID_LABEL] = vm_id
    if cluster:
        a["spec"]["clusterDeploymentName"] = {"namespace": cluster[0], "name": cluster[1]}
    if state:
        a["status"]["debugInfo"] = {"state": state}
    return a


# ---------------------------------------------------------------- hostname ---

def test_spec_hostname_wins_over_inventory():
    """spec.hostname is what the host will BE renamed to; status is what it is now."""
    a = _agent(spec_host="worker-01.lab", inv_host="localhost.localdomain")
    assert detect.agent_hostname(a) == "worker-01.lab"


def test_falls_back_to_reported_inventory_hostname():
    assert detect.agent_hostname(_agent(inv_host="worker-02.lab")) == "worker-02.lab"


def test_hostname_is_none_before_inventory_arrives():
    """Must be None, not '' and not the UUID -- callers render it as <no-inventory-yet>."""
    assert detect.agent_hostname(_agent()) is None
    assert detect.agent_hostname({}) is None


def test_display_name_falls_back_to_uid():
    assert detect.agent_display_name(_agent(inv_host="w3.lab")) == "w3.lab"
    assert detect.agent_display_name(_agent(uid="abc-123")) == "abc-123"


# ------------------------------------------------------------------- ident ---

def test_ident_contains_hostname_uid_namespace_vmid_and_cluster():
    a = _agent(uid="abc-123", ns="open-cluster-management", spec_host="worker-01.lab",
               vm_id="vm-042", cluster=("prod", "cluster-a"), state="installed")
    s = detect.agent_ident(a, config.VM_ID_LABEL)
    for expected in ["host=worker-01.lab", "uid=abc-123", "ns=open-cluster-management",
                     "vm-id=vm-042", "cluster=prod/cluster-a", "state=installed"]:
        assert expected in s, f"{expected!r} missing from {s!r}"


def test_ident_marks_missing_pieces_visibly_rather_than_silently():
    """An absent hostname must LOOK absent -- never silently render as the UUID."""
    s = detect.agent_ident(_agent(uid="abc-123"), config.VM_ID_LABEL)
    assert "host=<no-inventory-yet>" in s
    assert "vm-id=<UNLABELLED>" in s
    assert "cluster=<unbound>" in s


# ------------------------------------------------------------ agents_on_node ---

def test_agents_on_node_returns_only_that_cluster_on_that_node():
    c1, c2 = ("ns", "c1"), ("ns", "c2")
    a1 = _agent(uid="1", spec_host="h1", vm_id="v1", cluster=c1)
    a2 = _agent(uid="2", spec_host="h2", vm_id="v2", cluster=c1)
    a3 = _agent(uid="3", spec_host="h3", vm_id="v3", cluster=c2)   # other cluster
    a4 = _agent(uid="4", spec_host="h4", vm_id="v4", cluster=c1)   # other node
    nodes = {"v1": "node-A", "v2": "node-A", "v3": "node-A", "v4": "node-B"}
    got = detect.agents_on_node([a1, a2, a3, a4], nodes, config.VM_ID_LABEL, c1, "node-A")
    assert sorted(detect.agent_display_name(a) for a in got) == ["h1", "h2"]


def test_agents_on_node_ignores_unlabelled_agents():
    c = ("ns", "c1")
    unlabelled = _agent(uid="9", spec_host="h9", cluster=c)   # no vm-id label
    assert detect.agents_on_node([unlabelled], {"": "node-A"}, config.VM_ID_LABEL, c, "node-A") == []


# ------------------------------------------- check_orphan end-to-end logging ---

@pytest.fixture()
def caplog_info(caplog):
    caplog.set_level(logging.INFO, logger="agent-reconciler")
    return caplog


def _run_check(monkeypatch, body, vms=None, boom=None):
    logger = logging.getLogger("agent-reconciler")
    def fake_list_vms(api, label_selector=None):
        if boom:
            raise boom
        return vms or []
    monkeypatch.setattr(operator.clients, "list_vms", fake_list_vms)
    monkeypatch.setattr(operator, "_state", {"kv": object(), "mgmt": object()})
    operator._miss.clear()
    meta = body["metadata"]
    operator.check_orphan(name=meta["name"], namespace=meta["namespace"],
                          body=body, logger=logger)
    return logger


def test_orphan_line_names_the_host_not_just_the_uuid(monkeypatch, caplog_info):
    body = _agent(uid="2f8c1a34-dead-beef", spec_host="worker-07.lab",
                  vm_id="vm-042", cluster=("prod", "cluster-a"))
    for _ in range(config.MISS_THRESHOLD):
        _run_but_keep_state = None
        logger = logging.getLogger("agent-reconciler")
        monkeypatch.setattr(operator.clients, "list_vms", lambda api, label_selector=None: [])
        monkeypatch.setattr(operator, "_state", {"kv": object()})
        operator.check_orphan(name=body["metadata"]["name"],
                              namespace=body["metadata"]["namespace"],
                              body=body, logger=logger)
    errors = [r.getMessage() for r in caplog_info.records if r.levelno >= logging.ERROR]
    assert errors, "expected an ORPHAN CANDIDATE line"
    msg = errors[-1]
    assert "worker-07.lab" in msg
    assert "ORPHAN CANDIDATE" in msg
    assert "vm-042" in msg
    operator._miss.clear()


def test_present_vm_logs_which_vm_was_found(monkeypatch, caplog_info):
    body = _agent(spec_host="worker-08.lab", vm_id="vm-8")
    vms = [{"metadata": {"name": "vm-8", "namespace": "vms"}}]
    _run_check(monkeypatch, body, vms=vms)
    msgs = " | ".join(r.getMessage() for r in caplog_info.records)
    assert "worker-08.lab" in msgs and "vms/vm-8" in msgs


def test_query_failure_says_it_is_NOT_counting(monkeypatch, caplog_info):
    """The safety rule must be visible in the log, not just in the code."""
    body = _agent(spec_host="worker-09.lab", vm_id="vm-9")
    _run_check(monkeypatch, body, boom=RuntimeError("connection refused"))
    msgs = " | ".join(r.getMessage() for r in caplog_info.records)
    assert "worker-09.lab" in msgs
    assert "NOT counting" in msgs
    assert operator._miss == {}, "a failed query must never increment the miss streak"


def test_unlabelled_agent_is_skipped_with_its_hostname_named(monkeypatch, caplog_info):
    body = _agent(spec_host="worker-10.lab")   # no vm-id
    _run_check(monkeypatch, body)
    msgs = " | ".join(r.getMessage() for r in caplog_info.records)
    assert "worker-10.lab" in msgs and "SKIP" in msgs
