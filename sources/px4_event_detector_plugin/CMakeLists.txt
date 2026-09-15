cmake_minimum_required(VERSION 3.8)
project(px4_event_detector_plugin)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(cloud_native_robotics_interfaces REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(event_detector REQUIRED)
find_package(pluginlib REQUIRED)
find_package(px4_msgs REQUIRED)
find_package(rclcpp REQUIRED)

add_library(${PROJECT_NAME}_state_machines
  src/rule_state_machines.cpp
)
set_target_properties(${PROJECT_NAME}_state_machines PROPERTIES
  POSITION_INDEPENDENT_CODE ON
)
target_compile_features(${PROJECT_NAME}_state_machines PUBLIC cxx_std_17)
target_include_directories(${PROJECT_NAME}_state_machines PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)

add_library(${PROJECT_NAME} SHARED
  src/event_support.cpp
  src/rules/analytics_latency_slo_rule.cpp
  src/rules/battery_low_rule.cpp
  src/rules/telemetry_heartbeat_rule.cpp
)
target_compile_features(${PROJECT_NAME} PUBLIC cxx_std_17)
target_include_directories(${PROJECT_NAME} PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)
target_link_libraries(${PROJECT_NAME} ${PROJECT_NAME}_state_machines)
ament_target_dependencies(${PROJECT_NAME}
  cloud_native_robotics_interfaces
  diagnostic_msgs
  event_detector
  pluginlib
  px4_msgs
  rclcpp
)

pluginlib_export_plugin_description_file(event_detector plugins.xml)

install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY config DESTINATION share/${PROJECT_NAME})
install(FILES plugins.xml DESTINATION share/${PROJECT_NAME})
install(TARGETS ${PROJECT_NAME} ${PROJECT_NAME}_state_machines
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
)

ament_package()
