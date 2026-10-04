# Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS (kuberos-io/kuberos commit 0253c9e).
# See THIRD_PARTY_NOTICES (repository root; in the container images: /usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).
from .celery import app as celery_app

app = celery_app

__all__ = ("celery_app", "app")
