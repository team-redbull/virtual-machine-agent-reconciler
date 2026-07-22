"""All tunables come from the environment so nothing is hard-coded per cluster."""
import os


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# The shared join key. MUST match the label your GitOps stamps on BOTH the VM
# (KubeVirt) and the Agent (mgmt). There is no sensible default — set it.
VM_ID_LABEL = os.environ.get("VM_ID_LABEL", "infra.example.com/vm-id")

# Which inventories to manage. Only Agents whose inventory (InfraEnv) name
# CONTAINS this substring are checked — so physical / non-VM inventories are left
# alone and never mis-flagged as orphans. Change it if your naming differs.
INVENTORY_NAME_FILTER = os.environ.get("INVENTORY_NAME_FILTER", "virtual-machine")

# The label MCE stamps on an Agent to record its inventory (InfraEnv) name.
# Change if your version uses a different key.
INFRAENV_LABEL = os.environ.get("INFRAENV_LABEL", "infraenvs.agent-install.openshift.io")

# Path to the mounted kubeconfig granting read access to the KubeVirt cluster.
KUBEVIRT_KUBECONFIG = os.environ.get("KUBEVIRT_KUBECONFIG", "/etc/kubevirt/kubeconfig")

# Per-Agent orphan check cadence (seconds).
CHECK_INTERVAL = _int("CHECK_INTERVAL", 60)

# Global skew sweep cadence (seconds).
SKEW_INTERVAL = _int("SKEW_INTERVAL", 120)

# Consecutive *confirmed-absent* checks required before flagging an orphan.
# Guards against racing a mid-recreation (cattle replacement).
MISS_THRESHOLD = _int("MISS_THRESHOLD", 3)

# Max VMIs of a single cluster tolerated on one infra node before it's flagged.
MAX_SKEW = _int("MAX_SKEW", 1)

# Prometheus metrics port.
METRICS_PORT = _int("METRICS_PORT", 8080)
