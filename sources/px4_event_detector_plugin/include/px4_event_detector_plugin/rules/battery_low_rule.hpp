#pragma once

#include <deque>
#include <memory>
#include <mutex>
#include <string>

#include <event_detector/AnalysisRule.hpp>
#include <px4_msgs/msg/battery_status.hpp>
#include <px4_msgs/msg/vehicle_command.hpp>
#include <px4_msgs/msg/vehicle_command_ack.hpp>
#include <px4_msgs/msg/vehicle_status.hpp>
#include <rclcpp/rclcpp.hpp>

#include "cloud_native_robotics_interfaces/msg/operational_event.hpp"
#include "px4_event_detector_plugin/rule_state_machines.hpp"

namespace px4_event_detector_plugin {

class BatteryLowRule : public event_detector::AnalysisRule {
 public:
  std::string getRuleName() const override;
  void loadRuleParameters() override;
  void evaluate() override;

 protected:
  void onInitialize() override;

 private:
  struct BatteryObservation {
    double received_at_sec;
    double remaining;
    bool connected;
  };

  void handleEffects(const BatteryEffects& effects, double now_sec);
  void publishEvent(std::uint8_t state, std::uint8_t severity, const std::string& detail);
  void sendRtl(double now_sec);

  std::mutex mutex_;
  std::deque<BatteryObservation> battery_observations_;
  std::deque<AckStatus> acknowledgements_;
  std::size_t rtl_observations_{0};
  std::unique_ptr<BatteryStateMachine> state_machine_;
  BatteryConfig config_;

  std::string robot_id_;
  std::string battery_topic_;
  std::string command_topic_;
  std::string ack_topic_;
  std::string vehicle_status_topic_;
  std::string event_topic_;
  std::string correlation_id_;
  double latest_remaining_{0.0};
  int target_system_{1};
  int target_component_{1};
  int source_system_{1};
  int source_component_{1};

  rclcpp::Subscription<px4_msgs::msg::BatteryStatus>::SharedPtr battery_sub_;
  rclcpp::Subscription<px4_msgs::msg::VehicleCommandAck>::SharedPtr ack_sub_;
  rclcpp::Subscription<px4_msgs::msg::VehicleStatus>::SharedPtr vehicle_status_sub_;
  rclcpp::Publisher<px4_msgs::msg::VehicleCommand>::SharedPtr command_pub_;
  rclcpp::Publisher<cloud_native_robotics_interfaces::msg::OperationalEvent>::SharedPtr event_pub_;
};

}  // namespace px4_event_detector_plugin
