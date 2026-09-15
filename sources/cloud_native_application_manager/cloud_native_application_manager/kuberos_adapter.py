"""Small, explicit adapter for the KubeROS deployment REST API."""

import json
import ssl
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class KuberosError(RuntimeError):
    """KubeROS rejected an operation or returned an invalid response."""


class KuberosTimeout(KuberosError):
    """A deployment did not converge before its deadline."""


class KuberosOperationCancelled(KuberosError):
    """A wait operation was cancelled by the caller."""


@dataclass(frozen=True)
class OperationRef:
    operation_id: str
    deployment_id: str
    state: str


@dataclass(frozen=True)
class UpdateRef:
    event_id: str
    deployment_id: str
    target_revision: int
    state: str


class HttpTransport:
    """JSON transport based only on the Python standard library."""

    def __init__(self, verify_tls: bool = True):
        self._ssl_context = None if verify_tls else ssl._create_unverified_context()

    def request(self, method, url, headers, body, timeout):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(
                request, timeout=timeout, context=self._ssl_context
            ) as response:
                raw_body = response.read().decode("utf-8")
                return response.status, json.loads(raw_body) if raw_body else {}
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8")
            try:
                parsed = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                parsed = {"errors": [{"reason": "HTTPError", "msg": raw_body}]}
            return exc.code, parsed
        except URLError as exc:
            raise KuberosError(f"KubeROS is unreachable: {exc.reason}") from exc


class KuberosAdapter:
    """Translate policy operations into authenticated KubeROS requests."""

    DEPLOY_PATH = "/api/v1/deploying/deploy_rosmodule/"
    INFO_PATH = "/api/v1/deployment/deployments/{deployment_id}/"

    def __init__(
        self,
        base_url: str,
        token: str,
        transport=None,
        poll_interval_sec: float = 1.0,
        request_timeout_sec: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        if poll_interval_sec <= 0:
            raise ValueError("poll_interval_sec must be positive")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport or HttpTransport()
        self._poll_interval_sec = poll_interval_sec
        self._request_timeout_sec = request_timeout_sec
        self._monotonic = monotonic
        self._sleeper = sleeper

    def create_deployment(
        self, manifest: dict, correlation_id: str, rosparam_yamls=None
    ) -> OperationRef:
        deployment_manifest = deepcopy(manifest)
        metadata = deployment_manifest.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            raise ValueError("manifest metadata.name is required")
        metadata["correlationId"] = correlation_id
        body = {"deployment_manifest": deployment_manifest}
        if rosparam_yamls:
            body["rosparam_yamls"] = rosparam_yamls
        response = self._request("POST", self.DEPLOY_PATH, body, correlation_id)
        deployment_id = metadata["name"]
        return OperationRef(deployment_id, deployment_id, response["status"])

    def get_deployment(self, deployment_id: str, correlation_id: str = "") -> dict:
        path = self.INFO_PATH.format(deployment_id=quote(deployment_id, safe=""))
        response = self._request("GET", path, None, correlation_id)
        data = response.get("data")
        if not isinstance(data, dict):
            raise KuberosError("KubeROS deployment response has no data object")
        return data

    def update_deployment(
        self, manifest: dict, correlation_id: str, rosparam_yamls=None
    ) -> UpdateRef:
        deployment_manifest = deepcopy(manifest)
        metadata = deployment_manifest.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            raise ValueError("manifest metadata.name is required")
        metadata["correlationId"] = correlation_id
        deployment_id = metadata["name"]
        body = {"deployment_manifest": deployment_manifest}
        if rosparam_yamls:
            body["rosparam_yamls"] = rosparam_yamls
        path = self.DEPLOY_PATH + quote(deployment_id, safe="") + "/"
        response = self._request("PATCH", path, body, correlation_id)
        data = response.get("data")
        if not isinstance(data, dict):
            raise KuberosError("KubeROS update response has no data object")
        event_id = data.get("event_id")
        target_revision = data.get("target_revision")
        if not event_id or not isinstance(target_revision, int):
            raise KuberosError(
                "KubeROS update response has no event_id or target_revision"
            )
        return UpdateRef(
            event_id=str(event_id),
            deployment_id=deployment_id,
            target_revision=target_revision,
            state=response["status"],
        )

    def delete_deployment(
        self, deployment_id: str, correlation_id: str
    ) -> OperationRef:
        path = self.DEPLOY_PATH + quote(deployment_id, safe="") + "/"
        response = self._request("DELETE", path, None, correlation_id)
        return OperationRef(deployment_id, deployment_id, response["status"])

    def wait_ready(
        self,
        deployment_id: str,
        timeout_sec: float,
        correlation_id: str,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> dict:
        deadline = self._monotonic() + max(0.0, timeout_sec)
        last_state = "unknown"
        while self._monotonic() < deadline:
            if cancel_requested():
                raise KuberosOperationCancelled(
                    f"Wait for deployment '{deployment_id}' was cancelled"
                )
            data = self.get_deployment(deployment_id, correlation_id)
            last_state = str(data.get("status", "unknown")).lower()
            if last_state == "running":
                return data
            if last_state in {"failed", "deleted"}:
                raise KuberosError(
                    f"Deployment '{deployment_id}' reached state '{last_state}'"
                )
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleeper(min(self._poll_interval_sec, remaining))
        raise KuberosTimeout(
            f"Deployment '{deployment_id}' did not become ready; last state "
            f"was '{last_state}'"
        )

    def wait_revision(
        self,
        deployment_id: str,
        target_revision: int,
        timeout_sec: float,
        correlation_id: str,
        event_id: str = "",
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> dict:
        if target_revision < 1:
            raise ValueError("target_revision must be positive")
        deadline = self._monotonic() + max(0.0, timeout_sec)
        last_state = "unknown"
        last_revision = 0
        while self._monotonic() < deadline:
            if cancel_requested():
                raise KuberosOperationCancelled(
                    f"Wait for deployment '{deployment_id}' was cancelled"
                )
            data = self.get_deployment(deployment_id, correlation_id)
            last_state = str(data.get("status", "unknown")).lower()
            try:
                last_revision = int(data.get("revision", 0))
            except (TypeError, ValueError) as exc:
                raise KuberosError(
                    f"Deployment '{deployment_id}' returned an invalid revision"
                ) from exc

            events = data.get("deployment_event_set") or []
            matching_events = [
                item for item in events
                if (
                    (event_id and str(item.get("uuid", "")) == event_id)
                    or (
                        not event_id
                        and item.get("target_revision") == target_revision
                    )
                )
            ]
            failed_event = next(
                (
                    item for item in matching_events
                    if str(item.get("event_status", "")).lower() == "failed"
                ),
                None,
            )
            if failed_event:
                detail = failed_event.get("error_message") or "unspecified error"
                raise KuberosError(
                    f"Update of deployment '{deployment_id}' failed: {detail}"
                )
            if last_state in {"failed", "deleted"}:
                raise KuberosError(
                    f"Deployment '{deployment_id}' reached state '{last_state}'"
                )
            if last_state == "running" and last_revision >= target_revision:
                return data
            remaining = deadline - self._monotonic()
            if remaining > 0:
                self._sleeper(min(self._poll_interval_sec, remaining))
        raise KuberosTimeout(
            f"Deployment '{deployment_id}' did not reach revision "
            f"{target_revision}; last state was '{last_state}' at revision "
            f"{last_revision}"
        )

    def _request(self, method, path, body, correlation_id):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Token {self._token}"
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        status_code, response = self._transport.request(
            method,
            self._base_url + path,
            headers,
            body,
            self._request_timeout_sec,
        )
        if status_code not in {200, 202}:
            raise KuberosError(
                f"KubeROS returned HTTP {status_code}: {self._error_text(response)}"
            )
        if not isinstance(response, dict):
            raise KuberosError("KubeROS returned a non-object JSON response")
        state = str(response.get("status", "unknown")).lower()
        if state in {"failed", "rejected"}:
            raise KuberosError(self._error_text(response))
        if state not in {"success", "accepted"}:
            raise KuberosError(f"Unknown KubeROS response status '{state}'")
        return response

    @staticmethod
    def _error_text(response):
        if not isinstance(response, dict):
            return str(response)
        errors = response.get("errors") or []
        if errors:
            return "; ".join(
                f"{item.get('reason', 'error')}: {item.get('msg', '')}"
                for item in errors
            )
        return "; ".join(response.get("msgs") or []) or "unspecified error"
