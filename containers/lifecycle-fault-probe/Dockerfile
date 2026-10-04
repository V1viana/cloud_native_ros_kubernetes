# Test fixture image (checklist R2, retry test): only the lifecycle_fault_probe
# package, so the control-plane image that carries companion_analytics -- the
# workload variants A and B are compared on -- is not touched by it.
FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78 AS builder

WORKDIR /ws/src
COPY sources/lifecycle_fault_probe ./lifecycle_fault_probe

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
