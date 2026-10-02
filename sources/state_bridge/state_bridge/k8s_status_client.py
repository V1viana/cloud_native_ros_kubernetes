"""Minimal client for the ROSModule custom resource: GET spec, PATCH status.

Deliberately mirrors cloud_native_application_manager/kubernetes_adapter.py
(urllib + service-account token, no `kubernetes` pip dependency) instead of
the official Kubernetes Python client used by the Fleet Operator: that
client is fine for the Kopf-based operator container, but adding it here
would mean installing an extra pip package into the ROS-based onboard
image for a handful of GET/PATCH calls this project already knows how to
make with the standard library alone.

Also lists and watches the one ROSModule it serves (list_resource,
watch_resource), the proposal's "watch spec / patch status" channel (S4.2).
It used to poll only, to avoid holding a long-lived chunked HTTP connection
open from inside a ROS 2 callback; the watch now runs in spec_watch.py's own
plain thread, never inside the ROS executor (docs/CRD_CONTRACT_AUDIT.md, R4).
"""

from http.client import HTTPException
import json
import os
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .window_trace import NULL_DETAIL


class K8sStatusClientError(RuntimeError):
    """The Kubernetes API rejected an operation or returned invalid data."""


class Conflict(K8sStatusClientError):
    """HTTP 409: a resourceVersion precondition failed -- another writer came first.
    Re-read and re-evaluate; never retry the same body unconditionally."""


class WatchExpired(K8sStatusClientError):
    """410 Gone: the resourceVersion left the watch cache; list again, then watch."""


class K8sStatusClient:
    # Option 3 of the D6 diagnosis: set by bridge.py to the core's DetailRecorder
    # when the detail trace is on; records each request and each watch line parsed.
    detail = NULL_DETAIL
    SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
    GROUP = "dronekube.io"
    VERSION = "v1alpha1"
    PLURAL = "rosmodules"

    def __init__(self, base_url, token, namespace, ca_file=None, request_timeout_sec=10.0):
        if not base_url:
            raise ValueError("base_url is required")
        if not token:
            raise ValueError("service-account token is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._namespace = namespace
        self._ssl_context = ssl.create_default_context(cafile=ca_file)
        self._request_timeout_sec = request_timeout_sec

    @classmethod
    def from_service_account(cls, namespace=None):
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise K8sStatusClientError("KUBERNETES_SERVICE_HOST is not set")
        token_path = os.path.join(cls.SERVICE_ACCOUNT, "token")
        ca_path = os.path.join(cls.SERVICE_ACCOUNT, "ca.crt")
        with open(token_path, encoding="utf-8") as stream:
            token = stream.read().strip()
        if namespace is None:
            namespace_path = os.path.join(cls.SERVICE_ACCOUNT, "namespace")
            with open(namespace_path, encoding="utf-8") as stream:
                namespace = stream.read().strip()
        return cls(f"https://{host}:{port}", token, namespace, ca_file=ca_path)

    def get_spec(self, name):
        resource = self.get_resource(name)
        return resource.get("spec", {}), resource.get("metadata", {}).get("generation")

    def get_resource(self, name):
        return self._request("GET", self._resource_path(name), None)

    def patch_status(self, name, status_fields, resource_version=None):
        body = {"status": status_fields}
        if resource_version is not None:
            body["metadata"] = {"resourceVersion": resource_version}
        return self._request(
            "PATCH",
            self._resource_path(name) + "/status",
            body,
            content_type="application/merge-patch+json",
        )

    def list_resource(self, name):
        """This resource (None if absent) and the list's resourceVersion to watch from."""
        query = urlencode({"fieldSelector": f"metadata.name={name}"})
        body = self._request("GET", f"{self._collection_path()}?{query}", None)
        items = body.get("items", [])
        return (items[0] if items else None), body.get("metadata", {}).get("resourceVersion")

    def watch_resource(self, name, resource_version, timeout_sec=30):
        """Yield the watch events of this one resource until the server closes the
        stream (after timeout_sec). Raises WatchExpired on 410 Gone and
        K8sStatusClientError on any other failure, including a stalled stream."""
        query = urlencode({"fieldSelector": f"metadata.name={name}", "watch": "1",
                           "resourceVersion": resource_version or "",
                           "allowWatchBookmarks": "true",
                           "timeoutSeconds": str(int(timeout_sec))})
        request = Request(f"{self._base_url}{self._collection_path()}?{query}",
                          headers=self._headers("application/json"), method="GET")
        started = time.monotonic() if self.detail.enabled else None
        try:
            # The socket timeout outlives the server's own close by a margin, so a
            # stream that goes silent is detected instead of blocking forever.
            response = urlopen(request, timeout=timeout_sec + 15, context=self._ssl_context)
            if started is not None:
                self.detail.item("http", started, time.monotonic(), method="GET", ok=True, watch=True, status=False)
        except HTTPError as exc:
            if exc.code == 410:
                raise WatchExpired("HTTP 410: resourceVersion too old") from exc
            raise K8sStatusClientError(f"HTTP {exc.code}: watch refused") from exc
        except (URLError, OSError, HTTPException) as exc:
            raise K8sStatusClientError(f"watch failed to start: {exc}") from exc
        with response:
            try:
                for line in response:
                    if not line.strip():
                        continue
                    read = time.monotonic() if self.detail.enabled else None
                    event = json.loads(line)
                    if read is not None:
                        self.detail.item("watch_parse", read, time.monotonic(), bytes=len(line),
                                         type=event.get("type"))
                    if event.get("type") == "ERROR":
                        status = event.get("object") or {}
                        if status.get("code") == 410:
                            raise WatchExpired(status.get("message", "resourceVersion expired"))
                        raise K8sStatusClientError(f"watch error: {status.get('message')}")
                    yield event
            except (OSError, ValueError, HTTPException) as exc:
                raise K8sStatusClientError(f"watch stream interrupted: {exc}") from exc

    def _collection_path(self):
        return (f"/apis/{self.GROUP}/{self.VERSION}/namespaces/"
                f"{quote(self._namespace)}/{self.PLURAL}")

    def _headers(self, content_type):
        return {
            "Accept": "application/json",
            "Content-Type": content_type,
            "Authorization": f"Bearer {self._token}",
        }

    def _resource_path(self, name):
        return (
            f"/apis/{self.GROUP}/{self.VERSION}/namespaces/"
            f"{quote(self._namespace)}/{self.PLURAL}/{quote(name, safe='')}"
        )

    def _request(self, method, path, body, content_type="application/json"):
        if not self.detail.enabled:
            return self._request_once(method, path, body, content_type)
        started, ok = time.monotonic(), False
        try:
            result = self._request_once(method, path, body, content_type)
            ok = True
            return result
        finally:
            self.detail.item("http", started, time.monotonic(), method=method, ok=ok,
                             watch=False, status="status" in path.rsplit("/", 1)[-1])

    def _request_once(self, method, path, body, content_type):
        headers = self._headers(content_type)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self._base_url + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(
                request, timeout=self._request_timeout_sec, context=self._ssl_context
            ) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            message = raw
            try:
                message = json.loads(raw).get("message", raw) if raw else str(exc)
            except json.JSONDecodeError:
                pass
            error = Conflict if exc.code == 409 else K8sStatusClientError
            raise error(f"HTTP {exc.code}: {message}") from exc
        except URLError as exc:
            raise K8sStatusClientError(f"API server unreachable: {exc.reason}") from exc
        except (OSError, json.JSONDecodeError, HTTPException) as exc:
            raise K8sStatusClientError(f"API request failed: {exc}") from exc
