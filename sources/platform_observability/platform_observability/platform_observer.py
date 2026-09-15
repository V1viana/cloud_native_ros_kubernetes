"""Read-only Kubernetes observer producing durable platform snapshots."""

import json
import os
import re
import ssl
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from urllib.request import Request, urlopen

from .http_client import post_json


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class KubernetesSnapshotCollector:
    def __init__(self, base_url, token, namespace, ca_file=None, opener=urlopen):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._ssl_context = ssl.create_default_context(cafile=ca_file)
        self._opener = opener

    def collect(self):
        pods = self._get(f"/api/v1/namespaces/{quote(self._namespace)}/pods")
        deployments = self._get(
            f"/apis/apps/v1/namespaces/{quote(self._namespace)}/deployments"
        )
        events = self._get(f"/api/v1/namespaces/{quote(self._namespace)}/events")
        pod_items = pods.get("items", [])
        deployment_items = deployments.get("items", [])
        snapshot = {
            "namespace": self._namespace,
            "pods_total": len(pod_items),
            "pods_ready": sum(self._pod_ready(item) for item in pod_items),
            "deployments_total": len(deployment_items),
            "deployments_available": sum(
                self._deployment_available(item) for item in deployment_items
            ),
            "events_total": len(events.get("items", [])),
        }
        try:
            snapshot.update(self._collect_pod_metrics())
        except Exception as exc:
            snapshot.update(
                {
                    "metrics_available": False,
                    "metrics_error": str(exc),
                    "metrics_pods_total": 0,
                    "metrics_containers_total": 0,
                    "cpu_millicores_total": 0.0,
                    "memory_bytes_total": 0,
                    "pod_metrics": [],
                }
            )
        return snapshot

    def _collect_pod_metrics(self):
        metrics = self._get(
            f"/apis/metrics.k8s.io/v1beta1/namespaces/"
            f"{quote(self._namespace)}/pods"
        )
        pod_metrics = []
        containers_total = 0
        cpu_total = Decimal("0")
        memory_total = 0
        for item in metrics.get("items", []):
            pod_cpu = Decimal("0")
            pod_memory = 0
            containers = item.get("containers", [])
            for container in containers:
                usage = container.get("usage", {})
                pod_cpu += cpu_quantity_to_millicores(usage.get("cpu", "0"))
                pod_memory += memory_quantity_to_bytes(usage.get("memory", "0"))
            containers_total += len(containers)
            cpu_total += pod_cpu
            memory_total += pod_memory
            pod_metrics.append(
                {
                    "name": item.get("metadata", {}).get("name", ""),
                    "cpu_millicores": float(pod_cpu),
                    "memory_bytes": pod_memory,
                }
            )
        pod_metrics.sort(key=lambda item: item["name"])
        return {
            "metrics_available": True,
            "metrics_error": "",
            "metrics_pods_total": len(pod_metrics),
            "metrics_containers_total": containers_total,
            "cpu_millicores_total": float(cpu_total),
            "memory_bytes_total": memory_total,
            "pod_metrics": pod_metrics,
        }

    def _get(self, path):
        request = Request(
            self._base_url + path,
            headers={"Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        with self._opener(
            request,
            timeout=5.0,
            context=self._ssl_context,
        ) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _pod_ready(item):
        conditions = item.get("status", {}).get("conditions", [])
        return any(
            condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        )

    @staticmethod
    def _deployment_available(item):
        status = item.get("status", {})
        desired = item.get("spec", {}).get("replicas", 1)
        return status.get("availableReplicas", 0) >= desired


_QUANTITY_PATTERN = re.compile(
    r"^([+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$"
)


def _quantity_parts(value):
    match = _QUANTITY_PATTERN.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"invalid Kubernetes quantity: {value}")
    try:
        return Decimal(match.group(1)), match.group(2)
    except InvalidOperation as exc:
        raise ValueError(f"invalid Kubernetes quantity: {value}") from exc


def cpu_quantity_to_millicores(value):
    number, suffix = _quantity_parts(value)
    factors = {
        "": Decimal("1000"),
        "m": Decimal("1"),
        "u": Decimal("0.001"),
        "n": Decimal("0.000001"),
    }
    if suffix not in factors:
        raise ValueError(f"unsupported CPU quantity suffix: {suffix}")
    return number * factors[suffix]


def memory_quantity_to_bytes(value):
    number, suffix = _quantity_parts(value)
    binary = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "Pi": 1024 ** 5,
        "Ei": 1024 ** 6,
    }
    decimal = {
        "": 1,
        "k": 1000,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
        "P": 1000 ** 5,
        "E": 1000 ** 6,
    }
    factors = {**binary, **decimal}
    if suffix not in factors:
        raise ValueError(f"unsupported memory quantity suffix: {suffix}")
    return int(number * factors[suffix])


def service_account_collector(namespace):
    service_account = "/var/run/secrets/kubernetes.io/serviceaccount"
    with open(f"{service_account}/token", encoding="utf-8") as stream:
        token = stream.read().strip()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    return KubernetesSnapshotCollector(
        f"https://{host}:{port}",
        token,
        namespace,
        ca_file=f"{service_account}/ca.crt",
    )


def main():
    namespace = os.environ.get("KUBERNETES_NAMESPACE", "cloud-native-p2")
    audit_url = os.environ.get(
        "AUDIT_URL", "http://p2-audit-writer:8080/records"
    )
    interval = float(os.environ.get("POLL_INTERVAL_SEC", "15"))
    collector = service_account_collector(namespace)
    while True:
        try:
            snapshot = collector.collect()
            post_json(
                audit_url,
                {
                    "record_type": "platform_snapshot",
                    "timestamp_utc": utc_now(),
                    "correlation_id": f"platform-{namespace}",
                    "event_id": f"platform-{namespace}",
                    "robot_id": "fleet",
                    "event_type": "PlatformSnapshot",
                    "snapshot": snapshot,
                },
            )
            print(json.dumps(snapshot, sort_keys=True), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    {"component": "platform_observer", "error": str(exc)}
                ),
                flush=True,
            )
        time.sleep(interval)


if __name__ == "__main__":
    main()
