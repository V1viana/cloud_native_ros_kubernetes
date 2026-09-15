#pragma once

#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <utility>

#include <event_detector/AnalysisRule.hpp>
#include <rclcpp/rclcpp.hpp>

#include "cloud_native_robotics_interfaces/msg/metric_sample.hpp"
#include "cloud_native_robotics_interfaces/msg/operational_event.hpp"
#include "px4_event_detector_plugin/rule_state_machines.hpp"

namespace px4_event_detector_plugin {

class AnalyticsLatencySloRule : public event_detector::AnalysisRule {
 public:
  std::string getRuleName() const override;
  void loadRuleParameters() override;
  void evaluate() override;

 protected:
  void onInitialize() override;

 private:
  void publishEvent(std::uint8_t state, const LatencyEffects& effects);

  std::mutex mutex_;
  std::deque<std::pair<double, MetricObservation>> observations_;
  std::unique_ptr<LatencySloStateMachine> state_machine_;
  LatencyConfig config_;
  std::string robot_id_;
  std::string metrics_topic_;
  std::string event_topic_;
  std::string correlation_id_;

  rclcpp::Subscription<cloud_native_robotics_interfaces::msg::MetricSample>::SharedPtr metrics_sub_;
  rclcpp::Publisher<cloud_native_robotics_interfaces::msg::OperationalEvent>::SharedPtr event_pub_;
};

}  // namespace px4_event_detector_plugin
