"""RosMiddlewareAdapter (MAL): abstract vocabulary the State Bridge needs.

No import here may ever reference rclpy, ROS 2 message types or the `ros2`
CLI. ROS calls are implemented by Ros2MiddlewareAdapter. The bridge's
executor and Node wrapper still depend on rclpy.
"""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable


class LifecycleState(str, Enum):
    UNCONFIGURED = "Unconfigured"
    INACTIVE = "Inactive"
    ACTIVE = "Active"
    FINALIZED = "Finalized"
    UNKNOWN = "Unknown"


class MiddlewareUnavailableError(RuntimeError):
    """The underlying middleware could not complete the requested operation."""


class RosMiddlewareAdapter(ABC):
    """Vocabulary required by the State Bridge / Fleet Operator."""

    @abstractmethod
    def get_lifecycle_state(self, node_name: str) -> LifecycleState: ...

    @abstractmethod
    def set_lifecycle_state(
        self, node_name: str, target: LifecycleState,
        before_send: Callable[[LifecycleState], bool] = None,
        timeout_sec: float = None,
    ) -> bool: ...

    @abstractmethod
    def subscribe_metric(self, topic: str, callback: Callable[[Any], None]) -> None: ...

    @abstractmethod
    def subscribe_heartbeat(self, topic: str, callback: Callable[[], None]) -> None: ...

    @abstractmethod
    def subscribe_readiness(self, topic: str, callback: Callable[[bool], None]) -> None: ...

    @abstractmethod
    def call_service(self, service_name: str, request: Any) -> Any: ...
