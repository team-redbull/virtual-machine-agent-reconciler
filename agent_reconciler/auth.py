"""Explicit credentials for Kopf's own connection to the management cluster.

WHY THIS MODULE EXISTS
----------------------
Kopf's built-in fallback login handler `login_via_client` (kopf/_core/intents/
piggybacking.py) calls the kubernetes client's `load_incluster_config()` and then
reads the token back out of the shared default `Configuration` singleton:

    header = config.get_api_key_with_prefix('authorization')     # kopf <= 1.44.5

But kubernetes-client >= 36.0.2 stores the in-cluster ServiceAccount token under a
DIFFERENT api_key entry:

    client_configuration.api_key['BearerToken'] = "bearer <jwt>"  # incluster_config.py

So on kopf <= 1.44.5 + kubernetes >= 36.0.2 the lookup returns None, Kopf builds a
ConnectionInfo with `scheme=None, token=None`, and every request Kopf makes goes out
unauthenticated. The API server then reports:

    APIForbiddenError('forbidden: User "system:anonymous" cannot get path "/apis"')

Note the identity: `system:anonymous` means NO credential was sent. It is not RBAC.

kopf 1.44.6 fixed this upstream (it now tries 'BearerToken' first, then falls back to
'authorization'), and requirements.txt pins that floor. This module is the belt to that
suspenders: by registering an explicit `@kopf.on.login()` handler we take Kopf's
credential path off the shared `Configuration` singleton entirely, so the operator no
longer depends on which library pair the resolver happens to pick at build time, nor on
any other code in this process mutating that global.
"""
import dataclasses
import os

import kopf

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
HOST_ENV = "KUBERNETES_SERVICE_HOST"
PORT_ENV = "KUBERNETES_SERVICE_PORT"


def service_account_connection(
    sa_dir: str | None = None,
    environ=None,
    trust_env: bool = False,
) -> kopf.ConnectionInfo:
    """Build Kopf credentials by reading the projected ServiceAccount straight from disk.

    Deliberately does NOT touch `kubernetes.client.Configuration` — no global default is
    read or written, so nothing else in this process can influence the result.

    Raises a LoginError (never returns a token-less ConnectionInfo) so a broken mount
    fails loudly at login instead of degrading into a silent `system:anonymous` retry loop.
    """
    # Resolved at CALL time, not as a default argument: a default is bound once at
    # import, which would silently ignore any later override of SA_DIR (and did).
    sa_dir = SA_DIR if sa_dir is None else sa_dir
    environ = os.environ if environ is None else environ

    host = environ.get(HOST_ENV)
    port = environ.get(PORT_ENV)
    if not host or not port:
        raise kopf.LoginError(
            f"{HOST_ENV}/{PORT_ENV} are unset or empty -- not running inside a cluster, "
            f"so the in-cluster ServiceAccount login cannot be used."
        )

    token_path = os.path.join(sa_dir, "token")
    try:
        with open(token_path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        raise kopf.LoginError(
            f"Cannot read the ServiceAccount token at {token_path}: {e}. Check that the "
            f"pod has a ServiceAccount and that automountServiceAccountToken is not false."
        ) from e

    if not token:
        raise kopf.LoginError(
            f"The ServiceAccount token at {token_path} is empty. Sending this would make "
            f"every API call authenticate as system:anonymous."
        )

    ca_path = os.path.join(sa_dir, "ca.crt")
    ns_path = os.path.join(sa_dir, "namespace")
    namespace = None
    if os.path.exists(ns_path):
        with open(ns_path, encoding="utf-8") as f:
            namespace = f.read().strip() or None

    kwargs = dict(
        server=f"https://{host}:{port}",
        ca_path=ca_path if os.path.exists(ca_path) else None,
        scheme="Bearer",
        token=token,
        default_namespace=namespace,
    )

    # `trust_env` only exists on ConnectionInfo since kopf 1.44.2. Passing it
    # unconditionally would TypeError on older releases, which is the difference
    # between this module working back to ~1.38 and only working on the newest few.
    # The auth fields above -- the ones that actually matter -- are ancient and stable.
    if any(f.name == "trust_env" for f in dataclasses.fields(kopf.ConnectionInfo)):
        kwargs["trust_env"] = trust_env

    return kopf.ConnectionInfo(**kwargs)
