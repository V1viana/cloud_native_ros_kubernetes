# S2 test harness (R11): the local recorder and transient injector on drone01's
# node. Its own image, like the mission observer and the lifecycle fault probe:
# no project module changes to host it.
FROM ros:humble-ros-base AS builder
WORKDIR /ws/src
COPY interfaces/cloud_native_robotics_interfaces ./cloud_native_robotics_interfaces
COPY sources/s2_harness ./s2_harness
WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --cmake-args -DBUILD_TESTING=OFF

FROM ros:humble-ros-base
WORKDIR /ws
COPY --from=builder /ws/install /ws/install
ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
