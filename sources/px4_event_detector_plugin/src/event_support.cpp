#include "px4_event_detector_plugin/event_support.hpp"

#include <atomic>
#include <cctype>
#include <cstdint>
#include <sstream>
#include <utility>

#include <event_detector/EventDetector.hpp>

namespace px4_event_detector_plugin {
namespace {

std::atomic<std::uint64_t> event_sequence{0};

std::string identifierPart(std::string value) {
  for (char& character : value) {
    if (!std::isalnum(static_cast<unsigned char>(character)) && character != '-' && character != '_') {
      character = '-';
    }
  }
  return value;
}

}  // namespace

diagnostic_msgs::msg::KeyValue attribute(const std::string& key, const std::string& value) {
  diagnostic_msgs::msg::KeyValue item;
  item.key = key;
  item.value = value;
  return item;
}

std::string newCorrelationId(
    const std::string& robot_id, const std::string& event_type, double now_sec) {
  std::ostringstream stream;
  stream << identifierPart(robot_id) << '-' << identifierPart(event_type) << '-'
         << static_cast<std::uint64_t>(now_sec * 1000000.0) << '-' << ++event_sequence;
  return stream.str();
}

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
    std::vector<diagnostic_msgs::msg::KeyValue> attributes) {
  cloud_native_robotics_interfaces::msg::OperationalEvent event;
  const auto now = node->now();
  event.header.stamp = now;
  event.event_id = newCorrelationId(robot_id, event_type + "-event", now.seconds());
  event.correlation_id = correlation_id;
  event.source = "ros";
  event.robot_id = robot_id;
  event.component = component;
  event.event_type = event_type;
  event.severity = severity;
  event.state = state;
  event.observed_value = observed_value;
  event.threshold = threshold;
  event.window_sec = window_sec;
  event.attributes = std::move(attributes);
  return event;
}

}  // namespace px4_event_detector_plugin
