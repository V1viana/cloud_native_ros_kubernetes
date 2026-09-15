#include "px4_event_detector_plugin/rules/telemetry_heartbeat_rule.hpp"

#include <cstdint>
#include <utility>

#include <event_detector/EventDetector.hpp>
#include <pluginlib/class_list_macros.hpp>

#include "px4_event_detector_plugin/event_support.hpp"

namespace px4_event_detector_plugin {

std::string TelemetryHeartbeatRule::getRuleName() const {
  return "px4_event_detector_plugin::TelemetryHeartbeatRule";
}

void TelemetryHeartbeatRule::onInitialize() {}

void TelemetryHeartbeatRule::loadRuleParameters() {
  std::int64_t recovery_samples = 3;
  loadRuleParameter("robot_id", robot_id_, std::string("drone01"));
  loadRuleParameter("vehicle_status_topic", vehicle_status_topic_, std::string("/fmu/out/vehicle_status_v4"));
  loadRuleParameter("event_topic", event_topic_, std::string("/fleet/operational_events"));
  loadRuleParameter("startup_grace_sec", config_.startup_grace_sec, 60.0);
  loadRuleParameter("timeout_sec", config_.timeout_sec, 5.0);
  loadRuleParameter("recovery_samples", recovery_samples, std::int64_t{3});
  loadRuleParameter("recovery_window_sec", config_.recovery_window_sec, 3.0);
  loadRuleParameter("recovery_stability_sec", config_.recovery_stability_sec, 3.0);
  loadRuleParameter("cooldown_sec", config_.cooldown_sec, 60.0);
  config_.recovery_samples = static_cast<std::size_t>(recovery_samples);
  state_machine_ = std::make_unique<HeartbeatStateMachine>(config_, ed_->now().seconds());

  rclcpp::QoS event_qos(rclcpp::KeepLast(100));
  event_qos.reliable().transient_local();
  event_pub_ = ed_->create_publisher<cloud_native_robotics_interfaces::msg::OperationalEvent>(
      event_topic_, event_qos);
  vehicle_status_sub_ = ed_->create_subscription<px4_msgs::msg::VehicleStatus>(
      vehicle_status_topic_, rclcpp::SensorDataQoS(),
      [this](px4_msgs::msg::VehicleStatus::ConstSharedPtr) {
        std::lock_guard<std::mutex> lock(mutex_);
        heartbeat_times_.push_back(ed_->now().seconds());
      });
}

void TelemetryHeartbeatRule::evaluate() {
  std::deque<double> heartbeat_times;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    heartbeat_times.swap(heartbeat_times_);
  }

  for (const double heartbeat_time : heartbeat_times) {
    last_heartbeat_sec_ = heartbeat_time;
    const auto effects = state_machine_->processHeartbeat(heartbeat_time);
    if (effects.recovered) {
      publishEvent(
          cloud_native_robotics_interfaces::msg::OperationalEvent::STATE_RECOVERED, 0.0);
      correlation_id_.clear();
    }
  }

  const double now_sec = ed_->now().seconds();
  const auto effects = state_machine_->tick(now_sec);
  if (effects.entered) {
    correlation_id_ = newCorrelationId(robot_id_, "TelemetryHeartbeatLost", now_sec);
    publishEvent(
        cloud_native_robotics_interfaces::msg::OperationalEvent::STATE_ENTER,
        now_sec - last_heartbeat_sec_);
  }
}

void TelemetryHeartbeatRule::publishEvent(std::uint8_t state, double silence_sec) {
  using Event = cloud_native_robotics_interfaces::msg::OperationalEvent;
  const auto severity = state == Event::STATE_ENTER ? Event::SEVERITY_CRITICAL : Event::SEVERITY_INFO;
  event_pub_->publish(makeEvent(
      ed_, robot_id_, "microxrce-agent", "TelemetryHeartbeatLost", severity, state,
      silence_sec, config_.timeout_sec, config_.recovery_window_sec, correlation_id_,
      {
          attribute("vehicle_status_topic", vehicle_status_topic_),
          attribute("recovery_samples", std::to_string(config_.recovery_samples)),
          attribute("recovery_stability_sec", std::to_string(config_.recovery_stability_sec)),
          attribute("cooldown_sec", std::to_string(config_.cooldown_sec)),
      }));
}

}  // namespace px4_event_detector_plugin

PLUGINLIB_EXPORT_CLASS(
    px4_event_detector_plugin::TelemetryHeartbeatRule, event_detector::AnalysisRule)
