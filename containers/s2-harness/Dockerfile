# S2 test harness (R11): the local recorder and transient injector on drone01's
# node. Its own image, like the mission observer and the lifecycle fault probe:
# no project module changes to host it.
FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78 AS builder
WORKDIR /ws/src
COPY interfaces/cloud_native_robotics_interfaces ./cloud_native_robotics_interfaces
COPY sources/s2_harness ./s2_harness
WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --cmake-args -DBUILD_TESTING=OFF

FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78
WORKDIR /ws
COPY --from=builder /ws/install /ws/install
# License texts and attributions travel with the image (THIRD_PARTY_NOTICES).
COPY LICENSE THIRD_PARTY_NOTICES /usr/share/licenses/cloud-native-ros/
ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
