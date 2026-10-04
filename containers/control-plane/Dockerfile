FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78 AS builder

WORKDIR /ws/src
COPY integrations/px4_msgs ./px4_msgs
COPY interfaces/cloud_native_robotics_interfaces ./cloud_native_robotics_interfaces
COPY sources/cloud_native_application_manager ./cloud_native_application_manager
COPY sources/operational_event_dispatcher ./operational_event_dispatcher
COPY sources/e1_battery_fault_harness ./e1_battery_fault_harness
COPY sources/e0_kuberos_bootstrap ./e0_kuberos_bootstrap
COPY sources/companion_analytics ./companion_analytics
COPY sources/platform_observability ./platform_observability

WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --cmake-args -DBUILD_TESTING=OFF

# Runtime stage: this Dockerfile never runs rosdep (the base image already
# covers everything these packages need), so unlike event-detector's own
# runtime stage there is nothing extra to re-resolve here -- just drop the
# compiler toolchain, source tree and colcon's build/log directories that
# the single-stage version carried into the final image for no runtime
# benefit.
FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78

WORKDIR /ws
COPY --from=builder /ws/install /ws/install
# License texts and attributions travel with the image (THIRD_PARTY_NOTICES).
COPY LICENSE THIRD_PARTY_NOTICES /usr/share/licenses/cloud-native-ros/
COPY integrations/px4_msgs/LICENSE /usr/share/licenses/cloud-native-ros/px4_msgs/LICENSE

ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
