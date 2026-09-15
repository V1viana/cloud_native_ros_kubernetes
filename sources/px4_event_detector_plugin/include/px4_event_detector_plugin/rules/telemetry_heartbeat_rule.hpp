#pragma once

#include <deque>
#include <memory>
#include <mutex>
#include <string>

#include <event_detector/AnalysisRule.hpp>
#include <px4_msgs/msg/vehicle_status.hpp>
#include <rclcpp/rclcpp.hpp>

#include "cloud_native_robotics_interfaces/msg/operational_event.hpp"
#include "px4_event_detector_plugin/rule_state_machines.hpp"

namespace px4_event_detector_plugin {

class TelemetryHeartbeatRule : public event_detector::AnalysisRule {
 public:
  std::string getRuleName() const override;
  void loadRuleParameters() override;
  void evaluate() override;

 protected:
  void onInitialize() override;

 private:
  void publishEvent(std::uint8_t state, double silence_sec);

  std::mutex mutex_;
  std::deque<double> heartbeat_times_;
  std::unique_ptr<HeartbeatStateMachine> state_machine_;
  HeartbeatConfig config_;
  std::string robot_id_;
  std::string vehicle_status_topic_;
  std::string event_topic_;
  std::string correlation_id_;
  double last_heartbeat_sec_{0.0};

  rclcpp::Subscription<px4_msgs::msg::VehicleStatus>::SharedPtr vehicle_status_sub_;
  rclcpp::Publisher<cloud_native_robotics_interfaces::msg::OperationalEvent>::SharedPtr event_pub_;
};

}  // namespace px4_event_detector_plugin
