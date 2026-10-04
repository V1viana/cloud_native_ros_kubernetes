# Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS (kuberos-io/kuberos commit 0253c9e).
# See THIRD_PARTY_NOTICES (repository root; in the container images: /usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).
from django.urls import path, include

from .urls import v1_urls

app_name = 'main'

urlpatterns = [
    path('v1/', include(v1_urls)),
]