"""ROS 2 managed-node transitions used by the P2 blue/green switch."""

import time

from lifecycle_msgs.msg import State, Transition
from lifecycle_msgs.srv import ChangeState, GetState


class LifecycleTransitionError(RuntimeError):
    """A managed node did not reach the requested primary state."""


class RosLifecycleCoordinator:
    """Drive and verify Lifecycle services without shelling out to ros2cli."""

    def __init__(self, node, callback_group=None, poll_interval_sec=0.1):
        self._node = node
        self._callback_group = callback_group
        self._poll_interval_sec = poll_interval_sec

    def activate(self, node_name, timeout_sec, cancel_requested=lambda: False):
        deadline = time.monotonic() + max(0.0, timeout_sec)
        state = self._get_state(node_name, deadline, cancel_requested)
        if state == State.PRIMARY_STATE_UNCONFIGURED:
            self._change(
                node_name,
                Transition.TRANSITION_CONFIGURE,
                deadline,
                cancel_requested,
            )
            state = self._wait_state(
                node_name,
                State.PRIMARY_STATE_INACTIVE,
                deadline,
                cancel_requested,
            )
        if state == State.PRIMARY_STATE_INACTIVE:
            self._change(
                node_name,
                Transition.TRANSITION_ACTIVATE,
                deadline,
                cancel_requested,
            )
            state = self._wait_state(
                node_name,
                State.PRIMARY_STATE_ACTIVE,
                deadline,
                cancel_requested,
            )
        if state != State.PRIMARY_STATE_ACTIVE:
            raise LifecycleTransitionError(
                f"Lifecycle node '{node_name}' did not become Active; state={state}"
            )
        return "active"

    def deactivate(self, node_name, timeout_sec, cancel_requested=lambda: False):
        deadline = time.monotonic() + max(0.0, timeout_sec)
        state = self._get_state(node_name, deadline, cancel_requested)
        if state == State.PRIMARY_STATE_ACTIVE:
            self._change(
                node_name,
                Transition.TRANSITION_DEACTIVATE,
                deadline,
                cancel_requested,
            )
            state = self._wait_state(
                node_name,
                State.PRIMARY_STATE_INACTIVE,
                deadline,
                cancel_requested,
            )
        if state not in {
            State.PRIMARY_STATE_INACTIVE,
            State.PRIMARY_STATE_UNCONFIGURED,
        }:
            raise LifecycleTransitionError(
                f"Lifecycle node '{node_name}' did not become Inactive; state={state}"
            )
        return "inactive"

    def get_state(self, node_name, timeout_sec):
        return self._get_state(
            node_name,
            time.monotonic() + max(0.0, timeout_sec),
            lambda: False,
        )

    def _get_state(self, node_name, deadline, cancel_requested):
        client = self._client(node_name, GetState, "get_state", deadline)
        response = self._await(
            client.call_async(GetState.Request()),
            deadline,
            cancel_requested,
            node_name,
        )
        return response.current_state.id

    def _change(self, node_name, transition_id, deadline, cancel_requested):
        client = self._client(node_name, ChangeState, "change_state", deadline)
        request = ChangeState.Request()
        request.transition.id = transition_id
        response = self._await(
            client.call_async(request),
            deadline,
            cancel_requested,
            node_name,
        )
        if not response.success:
            raise LifecycleTransitionError(
                f"Lifecycle transition {transition_id} was rejected by '{node_name}'"
            )

    def _wait_state(self, node_name, expected, deadline, cancel_requested):
        state = 0
        while time.monotonic() < deadline:
            state = self._get_state(node_name, deadline, cancel_requested)
            if state == expected:
                return state
            time.sleep(self._poll_interval_sec)
        raise LifecycleTransitionError(
            f"Lifecycle node '{node_name}' did not reach state {expected}; "
            f"last state={state}"
        )

    def _client(self, node_name, service_type, suffix, deadline):
        service_name = f"/{node_name.strip('/')}/{suffix}"
        client = self._node.create_client(
            service_type,
            service_name,
            callback_group=self._callback_group,
        )
        remaining = max(0.0, deadline - time.monotonic())
        if not client.wait_for_service(timeout_sec=remaining):
            raise LifecycleTransitionError(
                f"Lifecycle service '{service_name}' is unavailable"
            )
        return client

    def _await(self, future, deadline, cancel_requested, node_name):
        while time.monotonic() < deadline:
            if cancel_requested():
                raise LifecycleTransitionError(
                    f"Lifecycle operation for '{node_name}' was cancelled"
                )
            if future.done():
                error = future.exception()
                if error is not None:
                    raise LifecycleTransitionError(str(error))
                return future.result()
            time.sleep(self._poll_interval_sec)
        raise LifecycleTransitionError(
            f"Lifecycle operation for '{node_name}' timed out"
        )
