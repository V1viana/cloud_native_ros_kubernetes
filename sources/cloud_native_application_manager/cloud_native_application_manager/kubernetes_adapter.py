"""Minimal in-cluster adapter for native Kubernetes remediation primitives."""

import json
import os
import ssl
import time
from copy import deepcopy
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class KubernetesError(RuntimeError):
    """The Kubernetes API rejected an operation or returned invalid data."""


class KubernetesTimeout(KubernetesError):
    """A Kubernetes workload did not converge before its deadline."""


class KubernetesHttpTransport:
    """Small JSON transport with service-account CA verification."""

    def __init__(self, ca_file=None):
        self._ssl_context = ssl.create_default_context(cafile=ca_file)

    def request(self, method, url, headers, body, timeout):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(
                request,
                timeout=timeout,
                context=self._ssl_context,
            ) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                response = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                response = {"message": raw}
            return exc.code, response
        except URLError as exc:
            raise KubernetesError(
                f"Kubernetes API is unreachable: {exc.reason}"
            ) from exc


class KubernetesAdapter:
    """Apply a bounded set of remediation operations with a bearer token."""

    SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(
        self,
        base_url,
        token,
        transport=None,
        poll_interval_sec=1.0,
        request_timeout_sec=10.0,
        monotonic=time.monotonic,
        sleeper=time.sleep,
        utc_now=lambda: datetime.now(timezone.utc),
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("service-account token is required")
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport or KubernetesHttpTransport()
        self._poll_interval_sec = poll_interval_sec
        self._request_timeout_sec = request_timeout_sec
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._utc_now = utc_now

    @classmethod
    def from_service_account(cls, poll_interval_sec=1.0):
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise KubernetesError("KUBERNETES_SERVICE_HOST is not set")
        token_path = os.path.join(cls.SERVICE_ACCOUNT, "token")
        ca_path = os.path.join(cls.SERVICE_ACCOUNT, "ca.crt")
        with open(token_path, encoding="utf-8") as stream:
            token = stream.read().strip()
        return cls(
            f"https://{host}:{port}",
            token,
            transport=KubernetesHttpTransport(ca_path),
            poll_interval_sec=poll_interval_sec,
        )

    def emit_incident_event(self, namespace, deployment_name, event):
        timestamp = self._utc_now().isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        body = {
            "apiVersion": "events.k8s.io/v1",
            "kind": "Event",
            "metadata": {
                "generateName": "telemetry-heartbeat-lost-",
                "namespace": namespace,
                "labels": {
                    "cloud-native-robotics.io/correlation-id": (
                        event.correlation_id
                    ),
                    "cloud-native-robotics.io/robot-id": event.robot_id,
                },
            },
            "eventTime": timestamp,
            "action": "RestartMicroXrceAgent",
            "reason": event.event_type,
            "regarding": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "namespace": namespace,
                "name": deployment_name,
            },
            "reportingController": (
                "cloud-native-robotics.io/application-manager"
            ),
            "reportingInstance": "application-manager",
            "note": (
                f"Incident {event.correlation_id}: telemetry heartbeat lost "
                f"for {event.robot_id}"
            ),
            "type": "Warning",
        }
        return self._request(
            "POST",
            f"/apis/events.k8s.io/v1/namespaces/{quote(namespace)}/events",
            body,
        )

    def restart_deployment(
        self,
        namespace,
        deployment_name,
        correlation_id,
    ):
        timestamp = self._utc_now().isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": timestamp,
                            "cloud-native-robotics.io/correlation-id": (
                                correlation_id
                            ),
                        }
                    }
                }
            }
        }
        path = self._deployment_path(namespace, deployment_name)
        return self._request(
            "PATCH",
            path,
            body,
            content_type="application/merge-patch+json",
        )

    def create_diagnostic_job(
        self,
        namespace,
        job_template,
        correlation_id,
    ):
        body = deepcopy(job_template)
        metadata = body.setdefault("metadata", {})
        metadata["namespace"] = namespace
        labels = metadata.setdefault("labels", {})
        labels["cloud-native-robotics.io/correlation-id"] = correlation_id
        template_metadata = body.setdefault("spec", {}).setdefault(
            "template", {}
        ).setdefault("metadata", {})
        template_metadata.setdefault("labels", {}).update(labels)
        response = self._request(
            "POST",
            f"/apis/batch/v1/namespaces/{quote(namespace)}/jobs",
            body,
        )
        name = response.get("metadata", {}).get("name")
        if not name:
            raise KubernetesError("Created Job response has no metadata.name")
        return name

    def get_deployment(self, namespace, deployment_name):
        return self._request(
            "GET",
            self._deployment_path(namespace, deployment_name),
            None,
        )

    def set_analytics_route(
        self,
        namespace,
        robot_id,
        active_instance,
        correlation_id,
    ):
        """Record the lifecycle-gated analytics route in a project ConfigMap."""
        name = f"analytics-routing-{robot_id}"
        body = {
            "metadata": {
                "labels": {
                    "cloud-native-robotics.io/robot-id": robot_id,
                },
                "annotations": {
                    "cloud-native-robotics.io/correlation-id": correlation_id,
                },
            },
            "data": {
                "active_instance": active_instance,
                "correlation_id": correlation_id,
            },
        }
        return self._request(
            "PATCH",
            (
                f"/api/v1/namespaces/{quote(namespace)}/configmaps/"
                f"{quote(name, safe='')}"
            ),
            body,
            content_type="application/merge-patch+json",
        )

    def create_analytics_hpa(
        self,
        namespace,
        deployment_name,
        robot_id,
        correlation_id,
        cpu_target=70,
    ):
        name = f"{deployment_name}-hpa"
        body = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "cloud-native-robotics.io/robot-id": robot_id,
                    "cloud-native-robotics.io/correlation-id": correlation_id,
                },
            },
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": deployment_name,
                },
                "minReplicas": 1,
                "maxReplicas": 3,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": int(cpu_target),
                            },
                        },
                    }
                ],
            },
        }
        response = self._request(
            "POST",
            f"/apis/autoscaling/v2/namespaces/{quote(namespace)}/horizontalpodautoscalers",
            body,
        )
        return response.get("metadata", {}).get("name", name)

    def delete_analytics_hpa(self, namespace, name):
        return self._request(
            "DELETE",
            (
                f"/apis/autoscaling/v2/namespaces/{quote(namespace)}/"
                f"horizontalpodautoscalers/{quote(name, safe='')}"
            ),
            {"propagationPolicy": "Background"},
        )

    def wait_deployment_ready(
        self,
        namespace,
        deployment_name,
        timeout_sec,
        cancel_requested=lambda: False,
    ):
        deadline = self._monotonic() + max(0.0, timeout_sec)
        last_status = {}
        while self._monotonic() < deadline:
            if cancel_requested():
                raise KubernetesError("Deployment rollout wait was cancelled")
            deployment = self.get_deployment(namespace, deployment_name)
            metadata = deployment.get("metadata", {})
            spec = deployment.get("spec", {})
            status = deployment.get("status", {})
            last_status = status
            generation = int(metadata.get("generation", 0))
            observed = int(status.get("observedGeneration", 0))
            desired = int(spec.get("replicas", 1))
            current = int(status.get("replicas", 0))
            updated = int(status.get("updatedReplicas", 0))
            ready = int(status.get("readyReplicas", 0))
            available = int(status.get("availableReplicas", 0))
            unavailable = int(status.get("unavailableReplicas", 0))
            if (
                observed >= generation
                and current == desired
                and updated >= desired
                and ready >= desired
                and available >= desired
                and unavailable == 0
            ):
                return deployment
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleeper(min(self._poll_interval_sec, remaining))
        raise KubernetesTimeout(
            f"Deployment '{deployment_name}' did not converge; last status "
            f"was {last_status}"
        )

    @staticmethod
    def namespace_from_service_account(default="default"):
        path = os.path.join(KubernetesAdapter.SERVICE_ACCOUNT, "namespace")
        try:
            with open(path, encoding="utf-8") as stream:
                return stream.read().strip() or default
        except FileNotFoundError:
            return default

    @staticmethod
    def _deployment_path(namespace, deployment_name):
        return (
            f"/apis/apps/v1/namespaces/{quote(namespace)}/deployments/"
            f"{quote(deployment_name, safe='')}"
        )

    def _request(self, method, path, body, content_type="application/json"):
        headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
            "Authorization": f"Bearer {self._token}",
        }
        status, response = self._transport.request(
            method,
            self._base_url + path,
            headers,
            body,
            self._request_timeout_sec,
        )
        if status not in {200, 201, 202}:
            message = response.get("message", response) if isinstance(
                response, dict
            ) else response
            raise KubernetesError(
                f"Kubernetes API returned HTTP {status}: {message}"
            )
        if not isinstance(response, dict):
            raise KubernetesError("Kubernetes API returned non-object JSON")
        return response
