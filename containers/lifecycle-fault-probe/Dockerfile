# Test fixture image (checklist R2, retry test): only the lifecycle_fault_probe
# package, so the control-plane image that carries companion_analytics -- the
# workload variants A and B are compared on -- is not touched by it.
FROM ros:humble-ros-base AS builder

WORKDIR /ws/src
COPY sources/lifecycle_fault_probe ./lifecycle_fault_probe

WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --cmake-args -DBUILD_TESTING=OFF

FROM ros:humble-ros-base

WORKDIR /ws
COPY --from=builder /ws/install /ws/install

ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
