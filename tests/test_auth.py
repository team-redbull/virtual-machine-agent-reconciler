"""Regression tests for the `system:anonymous` outage.

The bug: kopf's built-in `login_via_client` read the in-cluster bearer token from
`Configuration.api_key['authorization']`, but kubernetes-client >=36.0.2 stores it
under `api_key['BearerToken']`. Kopf therefore built a ConnectionInfo with
token=None and sent every request unauthenticated.

test_kopf_builtin_login_is_the_documented_hazard pins that behaviour so nobody
re-litigates whether the built-in fallback is safe -- it asserts against whatever
kopf is actually installed.
"""
import os

import kopf
import pytest

from agent_reconciler import auth


@pytest.fixture()
def sa(tmp_path):
    (tmp_path / "token").write_text("HEADER.PAYLOAD.SIG\n")
    (tmp_path / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\nx\n")
    (tmp_path / "namespace").write_text("agent-reconciler\n")
    return tmp_path


ENV = {"KUBERNETES_SERVICE_HOST": "10.0.0.1", "KUBERNETES_SERVICE_PORT": "443"}


def test_connection_carries_a_bearer_token(sa):
    """The whole point: scheme AND token must be populated."""
    conn = auth.service_account_connection(sa_dir=str(sa), environ=ENV)
    assert conn.server == "https://10.0.0.1:443"
    assert conn.scheme == "Bearer"
    assert conn.token == "HEADER.PAYLOAD.SIG"        # stripped of trailing newline
    assert conn.ca_path == os.path.join(str(sa), "ca.crt")
    assert conn.default_namespace == "agent-reconciler"


def test_does_not_touch_the_global_configuration_singleton(sa):
    """Must not read or write kubernetes.client.Configuration._default.

    Days of debugging went into suspecting that shared singleton. The fix is only
    worth anything if it is genuinely out of that path.
    """
    from kubernetes.client import Configuration

    Configuration.set_default(None)
    auth.service_account_connection(sa_dir=str(sa), environ=ENV)
    assert Configuration._default is None


def test_empty_token_fails_loudly_instead_of_going_anonymous(sa):
    """An empty token must raise, never yield a token-less ConnectionInfo."""
    (sa / "token").write_text("   \n")
    with pytest.raises(kopf.LoginError, match="empty"):
        auth.service_account_connection(sa_dir=str(sa), environ=ENV)


def test_missing_token_file_fails_loudly(tmp_path):
    with pytest.raises(kopf.LoginError, match="Cannot read"):
        auth.service_account_connection(sa_dir=str(tmp_path), environ=ENV)


def test_sa_dir_is_resolved_at_call_time(sa, monkeypatch):
    """SA_DIR must be overridable after import.

    It was originally a default argument (`sa_dir: str = SA_DIR`), which binds once at
    import time -- so patching auth.SA_DIR silently did nothing and the pod-side
    diagnostic could not be pointed at a test mount.
    """
    monkeypatch.setattr(auth, "SA_DIR", str(sa))
    monkeypatch.setattr(os, "environ", dict(ENV))
    assert auth.service_account_connection().token == "HEADER.PAYLOAD.SIG"


def test_missing_cluster_env_fails_loudly(sa):
    with pytest.raises(kopf.LoginError, match="not running inside a cluster"):
        auth.service_account_connection(sa_dir=str(sa), environ={})


def test_operator_registers_an_explicit_login_handler():
    """If this fails, kopf silently reverts to the broken built-in fallback."""
    from kopf._core.intents import causes

    from agent_reconciler import operator

    handlers = kopf.get_default_registry()._activities.get_all_handlers()
    logins = [h for h in handlers if h.activity == causes.Activity.AUTHENTICATION]
    explicit = [h for h in logins if not getattr(h, "_fallback", False)]
    assert [str(h.id) for h in explicit] == ["login"]
    operator._assert_kopf_login_is_explicit()   # must not raise


def test_kopf_builtin_login_is_the_documented_hazard(sa, monkeypatch):
    """Characterise the installed kopf's built-in fallback against kubernetes>=36.

    kopf <=1.44.5 -> token is None (the outage).
    kopf >=1.44.6 -> token is extracted correctly (upstream fix).
    Either way our explicit handler makes it moot; this test just keeps the
    knowledge honest and machine-checked instead of living in a handoff doc.
    """
    import kubernetes
    from kubernetes.config import incluster_config
    from kopf._core.intents.piggybacking import login_via_client

    monkeypatch.setattr(incluster_config, "SERVICE_TOKEN_FILENAME", str(sa / "token"))
    monkeypatch.setattr(incluster_config, "SERVICE_CERT_FILENAME", str(sa / "ca.crt"))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)

    kubernetes.config.load_incluster_config()
    cfg = kubernetes.client.Configuration.get_default_copy()

    # This is the root cause, asserted directly: the token exists, under 'BearerToken'.
    assert cfg.get_api_key_with_prefix("BearerToken")
    assert cfg.get_api_key_with_prefix("authorization") is None

    import logging
    class _S:
        class networking:
            trust_env = False
    conn = login_via_client(logger=logging.getLogger("t"), settings=_S())

    if tuple(int(x) for x in kopf.__version__.split(".")[:3]) >= (1, 44, 6):
        assert conn.token, "kopf>=1.44.6 should extract the token from BearerToken"
    else:
        assert conn.token is None, (
            "kopf<=1.44.5 is expected to yield token=None here; if this now passes a "
            "token, re-check the pin rationale in requirements.txt"
        )
