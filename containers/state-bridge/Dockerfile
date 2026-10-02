# State Bridge runs as a sidecar in the same Pod as the ROS 2 module it
# bridges (see k8s_workloads.py: SIDECAR_CONTAINER_NAME), sharing that Pod's
# network namespace so DDS discovery of the target Lifecycle node works
# without any extra configuration. Humble base to match the lifecycle_msgs
# ABI the rest of the onboard/control-plane images build against.
FROM ros:humble-ros-base AS builder

WORKDIR /ws/src
COPY integrations/px4_msgs ./px4_msgs
COPY interfaces/cloud_native_robotics_interfaces ./cloud_native_robotics_interfaces
COPY sources/state_bridge ./state_bridge

WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --cmake-args -DBUILD_TESTING=OFF

# Runtime stage: same reasoning as control-plane's own Dockerfile -- no
# rosdep step exists here either, so nothing beyond /ws/install is needed.
FROM ros:humble-ros-base

WORKDIR /ws
COPY --from=builder /ws/install /ws/install

ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
