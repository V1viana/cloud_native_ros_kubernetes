FROM ros:humble-ros-base AS builder

WORKDIR /ws/src
COPY integrations/px4_msgs ./px4_msgs
COPY interfaces/cloud_native_robotics_interfaces ./cloud_native_robotics_interfaces
COPY integrations/robotkube/perception_interfaces/perception_msgs ./perception_msgs
COPY integrations/robotkube/event_detector/event_detector ./event_detector
COPY patches/robotkube-event-detector/0001-humble-rosbag2-timestamp.patch /tmp/
RUN patch -d event_detector -p1 < /tmp/0001-humble-rosbag2-timestamp.patch

WORKDIR /ws
RUN apt-get update && \
    rosdep install --from-paths src --ignore-src -r -y --rosdistro humble && \
    rm -rf /var/lib/apt/lists/*
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install \
      --packages-up-to event_detector px4_msgs cloud_native_robotics_interfaces \
      --cmake-args -DBUILD_TESTING=OFF

WORKDIR /ws/src
COPY sources/px4_event_detector_plugin ./px4_event_detector_plugin
WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    . /ws/install/setup.sh && \
    colcon build --merge-install \
      --packages-select px4_event_detector_plugin \
      --cmake-args -DBUILD_TESTING=OFF

# Runtime stage: only /ws/install (colcon's merged build output) and the
# packages' own *runtime* (exec) dependencies are needed to actually run
# ros2 launch/lifecycle -- the compiler toolchain, source tree and colcon's
# build/log directories that made up most of this image's size (2.87GB)
# are build-time only. Re-resolving deps via rosdep --dependency-types=exec
# against the same (already-patched) sources, rather than hand-picking apt
# packages, keeps this in sync with whatever the packages' own package.xml
# actually declares.
FROM ros:humble-ros-base

WORKDIR /ws
COPY --from=builder /ws/src /tmp/src
RUN apt-get update && \
    rosdep install --from-paths /tmp/src --ignore-src -r -y --rosdistro humble --dependency-types=exec && \
    rm -rf /var/lib/apt/lists/* /tmp/src
COPY --from=builder /ws/install /ws/install

ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
