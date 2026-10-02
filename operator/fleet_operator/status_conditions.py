"""The one write path for ROSModule.status.conditions.

ROSModuleController and LifecycleController both keep conditions on the same ROSModule.
Handing the whole list to Kopf lost one of them: Kopf merge-patches the status without a
resourceVersion precondition, a merge patch replaces lists, and the second writer's stale
copy silently won -- offline and on a real API server, in both orders, with a condition
even reverting to its old value (results/evidence/runs/CONDITIONS_CONCURRENCY.md).

Here each controller writes only the condition types it owns, merged into the latest
read and patched with that read's resourceVersion. A 409 repeats only this read-merge-
write, never the handler's other effects. If the ROSModule's uid or generation changed
since the handler read it, the conditions computed from the old object are dropped: the
next reconcile computes them again.
"""

from kubernetes import client

from . import lifecycle_commands as lc
from .conditions import build_condition, upsert_condition
from .constants import GROUP, VERSION

ROSMODULE_OWNED = ("Reconciled", "UpdateSucceeded", "ServiceReconciled")
LIFECYCLE_OWNED = ("LifecycleSettled", "Converged")
MAX_ATTEMPTS = 5


class ModuleStatusClient:
    """GET the ROSModule, merge-patch its status with a resourceVersion precondition;
    409 -> lc.Conflict. The content type is explicit, whatever the kubernetes client
    version would choose."""

    def __init__(self, namespace):
        self._namespace = namespace

    def get_resource(self, name):
        return client.CustomObjectsApi().get_namespaced_custom_object(
            GROUP, VERSION, self._namespace, "rosmodules", name)

    def patch_status(self, name, fields, resource_version=None):
        body = {"status": fields}
        if resource_version is not None:
            body["metadata"] = {"resourceVersion": resource_version}
        try:
            return client.ApiClient().call_api(
                f"/apis/{GROUP}/{VERSION}/namespaces/{{namespace}}/rosmodules/{{name}}/status", "PATCH",
                path_params={"namespace": self._namespace, "name": name},
                header_params={"Content-Type": "application/merge-patch+json", "Accept": "application/json"},
                body=body, response_type="object", auth_settings=["BearerToken"],
                _return_http_data_only=True)
        except client.ApiException as exc:
            if exc.status == 409:
                raise lc.Conflict(f"HTTP 409: {exc.reason}") from exc
            raise


def merge_owned(current, computed, owned):
    """`current` with each owned type replaced by its entry in `computed` (kept as is
    when `computed` has none); other types untouched. lastTransitionTime survives
    when the status did not change, as upsert_condition does."""
    merged = [dict(c) for c in current or []]
    for entry in computed:
        if entry.get("type") in owned:
            upsert_condition(merged, entry["type"], entry.get("status") == "True",
                             reason=entry.get("reason", ""), message=entry.get("message", ""))
    return merged


def _same(a, b):
    keys = ("type", "status", "reason", "message")
    return [tuple(c.get(k) for k in keys) for c in a] == [tuple(c.get(k) for k in keys) for c in b]


def write_conditions(api, name, computed, owned, *, uid, generation, logger,
                     attempts=MAX_ATTEMPTS, derive=None, on_written=None):
    """Persist the owned conditions. Returns "written", "unchanged", "stale" (uid or
    generation changed: computed from an old object), "gone" (404) or "conflict"
    (still 409 after `attempts` reads).

    derive(current) adds conditions computed from each fresh read (Converged: the
    other fields of the status may arrive before or after the conditions);
    on_written(previous, merged) runs only after a successful conditional write."""
    for _ in range(attempts):
        try:
            current = api.get_resource(name)
        except client.ApiException as exc:
            if exc.status == 404:
                return "gone"
            raise
        metadata = current.get("metadata") or {}
        if metadata.get("uid") != uid or metadata.get("generation") != generation:
            logger.info("ROSModule %s changed (uid/generation) since this reconcile read it: "
                        "its conditions are left to the next reconcile", name)
            return "stale"
        existing = (current.get("status") or {}).get("conditions") or []
        merged = merge_owned(existing, list(computed) + list(derive(current) if derive else []), owned)
        if _same(merged, existing):
            return "unchanged"
        try:
            api.patch_status(name, {"conditions": merged}, metadata.get("resourceVersion"))
        except lc.Conflict:
            continue
        if on_written:
            on_written(existing, merged)
        return "written"
    logger.warning("ROSModule %s: conditions not written, still in conflict after %d reads; "
                   "the next reconcile writes them", name, attempts)
    return "conflict"


def converged_condition(current, settled, settled_reason):
    """Converged, judged on the fresh read of the ROSModule plus this tick's
    LifecycleSettled (docs/F2_EVENTS_PROPOSAL.md, v4): the first missing requirement is
    the reason. ServiceReconciled counts only with a declared spec.service."""
    metadata, spec = current.get("metadata") or {}, current.get("spec") or {}
    status = current.get("status") or {}
    conditions = {c.get("type"): c for c in status.get("conditions") or []}

    def true(kind):
        return (conditions.get(kind) or {}).get("status") == "True"
    if status.get("observedGeneration") != metadata.get("generation"):
        reason = "GenerationPending"
    elif not true("Reconciled"):
        reason = "NotReconciled"
    elif status.get("updateState") != "Stable":
        reason = f"Update{status.get('updateState') or 'Pending'}"
    elif spec.get("service") and not true("ServiceReconciled"):
        reason = "ServiceNotReconciled"
    elif not settled:
        reason = f"Lifecycle{settled_reason}"
    else:
        return build_condition("Converged", True, reason="Converged",
                               message=f"generation {metadata.get('generation')} applied and "
                                       f"lifecycle at {spec.get('lifecycleTarget')}")
    return build_condition("Converged", False, reason=reason, message="")


def converged_change(previous, merged):
    """("Reconciled", "Normal", message) on entering Converged=True, ("ConvergenceLost",
    "Warning", reason) on leaving it, else None."""
    def entry(conditions):
        return next((c for c in conditions if c.get("type") == "Converged"), {})
    before, after = entry(previous), entry(merged)
    if after.get("status") == "True" and before.get("status") != "True":
        return "Reconciled", "Normal", after.get("message") or "converged"
    if before.get("status") == "True" and after.get("status") != "True":
        return "ConvergenceLost", "Warning", after.get("reason") or "no longer converged"
    return None
