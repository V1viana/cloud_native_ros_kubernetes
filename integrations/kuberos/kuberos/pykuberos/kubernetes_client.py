import base64
import json
from kubernetes import client
from kubernetes.client.rest import ApiException
from pykuberos.kuberos_executer import KubeConfig

class KubernetesClient:
    def __init__(self, kube_config: dict):
        cfg = KubeConfig(kube_config).cluster_config
        self._api_client = client.ApiClient(cfg)
        self._core = client.CoreV1Api(self._api_client)

    def close(self):
        try:
            self._api_client.close()
        except Exception:
            pass

    def get_container_access_token(self, namespace: str, secret_name: str, docker_config_json=None):
        try:
            sec = self._core.read_namespaced_secret(name=secret_name, namespace=namespace)
        except ApiException as e:
            if getattr(e, "status", None) == 404:
                return False, f"Secret '{secret_name}' not found in namespace '{namespace}'."
            return False, f"Kubernetes API error: {e}"

        data = sec.data or {}
        b64 = data.get(".dockerconfigjson")
        if not b64:
            return False, "Secret exists but does not contain .dockerconfigjson"

        try:
            raw = base64.b64decode(b64).decode("utf-8")
            return True, json.loads(raw)
        except Exception as e:
            return False, f"Failed to decode secret: {e}"

    def create_or_update_container_access_token(self, namespace: str, secret_name: str, docker_config_json: dict, update: bool = False):
        payload = base64.b64encode(json.dumps(docker_config_json).encode("utf-8")).decode("utf-8")
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name),
            type="kubernetes.io/dockerconfigjson",
            data={".dockerconfigjson": payload},
        )

        try:
            self._core.read_namespaced_secret(name=secret_name, namespace=namespace)
            if update:
                self._core.patch_namespaced_secret(name=secret_name, namespace=namespace, body=body)
                return True, f"Secret '{secret_name}' updated."
            return False, f"Secret '{secret_name}' already exists (use update)."
        except ApiException as e:
            if getattr(e, "status", None) != 404:
                return False, f"Kubernetes API error: {e}"

        try:
            self._core.create_namespaced_secret(namespace=namespace, body=body)
            return True, f"Secret '{secret_name}' created."
        except ApiException as e:
            return False, f"Kubernetes API error: {e}"
