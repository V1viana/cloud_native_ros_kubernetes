FROM ros:humble-ros-base

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

ENTRYPOINT ["/ros_entrypoint.sh"]
CMD ["bash"]
