"""CPU/memory from metrics-server as decision context (docs/CRD_CONTRACT_AUDIT.md, R8).

Proposal S4: the AdaptationController reads per-Pod CPU/memory from
metrics-server "to enrich the SLO evaluation with infrastructure evidence,
exactly as the existing Platform Observer does for the imperative baseline",
"never alone sufficient to trigger an action". So this is read only at
decision points (an incident starts or ends) and written into the audit
record and the policy status; the trigger and recovery logic never read it.
Read-only by construction: no write call, replicas stay the HPA's.

Pods are the module's current ones, by the same UID ownership chain readiness
uses (lifecycle_controller._current_pods). Quantities are converted exactly
as variant A's Platform Observer does (platform_observability/
platform_observer.py, copied: the operator image does not ship that package),
so both variants read metrics-server the same way. Any failure yields
"available": false with the error, and the decision goes on unchanged.

Bounded (review of 7f2a55b, P1): every API call carries a (connect, read)
timeout taken from what is left of one overall budget, on a client that does
not retry, so a request left hanging -- not only a refused one -- cannot hold
the reconcile once the action has been taken; the audit record and the status
still get written, with available=false. The no-retry client is not optional:
urllib3 retries a timed-out GET three times, and measured against a slow
server a 1s timeout became 10s (single number) or 4s (pair); live, a frozen
metrics-server held a reconcile 35s with timeouts alone.

A hard deadline for the caller (review of f21c5b6, P2): connect and read
timeouts used to be granted separately, so a call honouring both could take
the collection past the budget and still return available=true. Now each
call's connect + read never exceeds what is left, and the collection runs in a
worker thread the caller waits for at most budget_sec: past it the caller gets
available=false and moves on. An abandoned worker ends on its own timeouts --
except against a server trickling bytes, since urllib3's read timeout bounds
each socket read, not the whole response; that one only costs the thread, not
the reconcile.

For RestartComponent the context is the one of the observed module (the
ROSModule whose bridge reports the metric), not of the restarted component,
which is not a ROSModule: measuring that one is a separate extension.
"""

from decimal import Decimal, InvalidOperation
import re
import threading
import time

from kubernetes import client

from .k8s_workloads import OWNER_LABEL, deployment_name
from .lifecycle_controller import _current_pods

_QUANTITY_PATTERN = re.compile(
    r"^([+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$"
)
_CPU = {"": Decimal("1000"), "m": Decimal("1"), "u": Decimal("0.001"), "n": Decimal("0.000001")}
_MEMORY = {
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    "": 1, "k": 1000, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4,
    "P": 1000 ** 5, "E": 1000 ** 6,
}
_MAX_ERROR = 200
CONTEXT_BUDGET_SEC = 3.0
# Every field a context may carry: the status copy must clear the ones a new
# decision does not set (merge patch keeps omitted fields).
CONTEXT_FIELDS = ("source", "available", "error", "pods", "podsWithoutMetrics",
                  "cpuMillicoresTotal", "memoryBytesTotal")


def _quantity_parts(value):
    match = _QUANTITY_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"invalid Kubernetes quantity: {value}")
    try:
        return Decimal(match.group(1)), match.group(2)
    except InvalidOperation as exc:
        raise ValueError(f"invalid Kubernetes quantity: {value}") from exc


def cpu_millicores(value):
    number, suffix = _quantity_parts(value)
    if suffix not in _CPU:
        raise ValueError(f"unsupported CPU quantity suffix: {suffix}")
    return float(number * _CPU[suffix])


def memory_bytes(value):
    number, suffix = _quantity_parts(value)
    if suffix not in _MEMORY:
        raise ValueError(f"unsupported memory quantity suffix: {suffix}")
    return int(number * _MEMORY[suffix])


def _api_client():
    """A client for context reads only: same configuration, no retries."""
    config = client.Configuration.get_default_copy()
    config.retries = False
    return client.ApiClient(config)


def _module_pods(namespace, name, left, api_client):
    """(current Pod names, their metrics-server items) for one ROSModule; `left()`
    is the per-call (connect, read) timeout, from what remains of the budget."""
    apps = client.AppsV1Api(api_client)
    deployment_name_ = deployment_name(name)
    try:
        deployment = apps.read_namespaced_deployment(
            deployment_name_, namespace, _request_timeout=left())
    except client.ApiException as exc:
        if exc.status == 404:
            return set(), []
        raise
    selector = f"{OWNER_LABEL}={deployment_name_}"
    replicasets = apps.list_namespaced_replica_set(
        namespace, label_selector=selector, _request_timeout=left()).items
    pods = client.CoreV1Api(api_client).list_namespaced_pod(
        namespace, label_selector=selector, _request_timeout=left()).items
    current = {pod.metadata.name for pod in _current_pods(deployment, replicasets, pods)}
    metrics = client.CustomObjectsApi(api_client).list_namespaced_custom_object(
        "metrics.k8s.io", "v1beta1", namespace, "pods", label_selector=selector,
        _request_timeout=left()).get("items", [])
    return current, metrics


def infrastructure_context(namespace, module_names, budget_sec=CONTEXT_BUDGET_SEC):
    """Per-Pod CPU/memory of these modules' current Pods, as metrics-server reports
    them; never takes the caller more than budget_sec."""
    deadline = time.monotonic() + budget_sec
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.update(context=_collect(namespace, module_names, deadline, budget_sec)),
        name="decision-context", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - time.monotonic()))
    if "context" not in outcome:
        return {"source": "metrics-server", "available": False,
                "error": f"context budget of {budget_sec:g}s exceeded"}
    return outcome["context"]


def _collect(namespace, module_names, deadline, budget_sec):
    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"context budget of {budget_sec:g}s exhausted")
        connect = min(1.0, remaining / 2)
        return (connect, remaining - connect)

    try:
        api_client = _api_client()
        pods, without = [], []
        for name in module_names:
            current, metrics = _module_pods(namespace, name, left, api_client)
            reported = set()
            for item in metrics:
                pod_name = item.get("metadata", {}).get("name", "")
                if pod_name not in current:
                    continue
                reported.add(pod_name)
                usage = [c.get("usage", {}) for c in item.get("containers", [])]
                pods.append({
                    "module": name, "pod": pod_name,
                    "cpuMillicores": sum(cpu_millicores(u.get("cpu", "0")) for u in usage),
                    "memoryBytes": sum(memory_bytes(u.get("memory", "0")) for u in usage),
                    "timestamp": item.get("timestamp", ""), "window": item.get("window", ""),
                })
            without.extend(sorted(current - reported))
        pods.sort(key=lambda p: (p["module"], p["pod"]))
        return {
            "source": "metrics-server", "available": True, "pods": pods,
            "podsWithoutMetrics": without,
            "cpuMillicoresTotal": sum(p["cpuMillicores"] for p in pods),
            "memoryBytesTotal": sum(p["memoryBytes"] for p in pods),
        }
    except Exception as exc:  # never blocks or changes a decision
        return {"source": "metrics-server", "available": False, "error": str(exc)[:_MAX_ERROR]}
