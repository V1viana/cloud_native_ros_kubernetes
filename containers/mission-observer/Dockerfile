# Measurement instrument image (checklist R8, mission continuity): only
# px4_msgs and the mission_observer package, so the control-plane image that
# carries companion_analytics -- the workload variants A and B are compared
# on -- is not touched by it. px4_msgs is built in its own layer so a change
# to the observer does not rebuild the messages.
FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78 AS builder

WORKDIR /ws/src
COPY integrations/px4_msgs ./px4_msgs

WORKDIR /ws
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --packages-select px4_msgs \
      --cmake-args -DBUILD_TESTING=OFF

COPY sources/mission_observer ./src/mission_observer
RUN . /opt/ros/humble/setup.sh && \
    colcon build --merge-install --packages-select mission_observer

FROM ros:humble-ros-base@sha256:5417e56962ff6e15d4cf9b2f78a71a78f3901f47cd78696b575b7eecdb54eb78

WORKDIR /ws
COPY --from=builder /ws/install /ws/install
# License texts and attributions travel with the image (THIRD_PARTY_NOTICES).
COPY LICENSE THIRD_PARTY_NOTICES /usr/share/licenses/cloud-native-ros/
COPY integrations/px4_msgs/LICENSE /usr/share/licenses/cloud-native-ros/px4_msgs/LICENSE

ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
