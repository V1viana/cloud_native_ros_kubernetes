#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <diagnostic_msgs/msg/key_value.hpp>

#include "cloud_native_robotics_interfaces/msg/operational_event.hpp"

namespace event_detector {
class EventDetector;
}

namespace px4_event_detector_plugin {

diagnostic_msgs::msg::KeyValue attribute(const std::string& key, const std::string& value);

std::string newCorrelationId(
    const std::string& robot_id, const std::string& event_type, double now_sec);

cloud_native_robotics_interfaces::msg::OperationalEvent makeEvent(
    event_detector::EventDetector* node,
    const std::string& robot_id,
    const std::string& component,
    const std::string& event_type,
    std::uint8_t severity,
    std::uint8_t state,
    double observed_value,
    double threshold,
    double window_sec,
    const std::string& correlation_id,
    std::vector<diagnostic_msgs::msg::KeyValue> attributes = {});

}  // namespace px4_event_detector_plugin
