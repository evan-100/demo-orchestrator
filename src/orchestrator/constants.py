"""Shared constants for the Demo Environment Orchestrator.

Label, annotation, and API-group strings live only here. Import them,
never retype them elsewhere in the codebase.
"""

GROUP = "orchestrator.local"
VERSION = "v1alpha1"
PLURAL = "demoenvironments"
KIND = "DemoEnvironment"

NS_PREFIX = "demo-"

LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "demo-orchestrator"
LABEL_ENV = f"{GROUP}/env"
LABEL_PERSONA = f"{GROUP}/persona"

ANNOTATION_EXPIRES_AT = f"{GROUP}/expires-at"

# The operator's finalizer on DemoEnvironments (kopf persistence.finalizer). The
# sweeper strips it from a CR stuck in deletion while the operator is down.
FINALIZER = f"{GROUP}/finalizer"

PROTECTED_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "ingress-nginx",
        "demo-orchestrator",
        "local-path-storage",
    }
)

HARD_MAX_TTL_SECONDS = 8 * 3600
