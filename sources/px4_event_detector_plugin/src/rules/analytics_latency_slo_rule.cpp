#include "px4_event_detector_plugin/rules/analytics_latency_slo_rule.hpp"

#include <cstdint>
#include <utility>

#include <event_detector/EventDetector.hpp>
#include <pluginlib/class_list_macros.hpp>

#include "px4_event_detector_plugin/event_support.hpp"

namespace px4_event_detector_plugin {

std::string AnalyticsLatencySloRule::getRuleName() const {
  return "px4_event_detector_plugin::AnalyticsLatencySloRule";
}

void AnalyticsLatencySloRule::onInitialize() {}

void AnalyticsLatencySloRule::loadRuleParameters() {
  std::int64_t consecutive_windows = 3;
  loadRuleParameter("robot_id", robot_id_, std::string("drone01"));
  loadRuleParameter("metrics_topic", metrics_topic_, std::string("/drone01/analytics/metrics"));
  loadRuleParameter("event_topic", event_topic_, std::string("/fleet/operational_events"));
  loadRuleParameter("violation_threshold_ms", config_.violation_threshold_ms, 250.0);
  loadRuleParameter("recovery_threshold_ms", config_.recovery_threshold_ms, 150.0);
  loadRuleParameter("window_sec", config_.window_sec, 10.0);
  loadRuleParameter("consecutive_windows", consecutive_windows, std::int64_t{3});
  config_.consecutive_windows = static_cast<std::size_t>(consecutive_windows);
  state_machine_ = std::make_unique<LatencySloStateMachine>(config_);

  rclcpp::QoS event_qos(rclcpp::KeepLast(100));
  event_qos.reliable().transient_local();
  event_pub_ = ed_->create_publisher<cloud_native_robotics_interfaces::msg::OperationalEvent>(
      event_topic_, event_qos);
  metrics_sub_ = ed_->create_subscription<cloud_native_robotics_interfaces::msg::MetricSample>(
      metrics_topic_, rclcpp::QoS(50).reliable(),
      [this](cloud_native_robotics_interfaces::msg::MetricSample::ConstSharedPtr message) {
        if (!message->robot_id.empty() && message->robot_id != robot_id_) {
          return;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        observations_.emplace_back(
            ed_->now().seconds(),
            MetricObservation{message->latency_ms, message->queue_depth, message->cpu_percent});
      });
}

void AnalyticsLatencySloRule::evaluate() {
  std::deque<std::pair<double, MetricObservation>> observations;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    observations.swap(observations_);
  }
  for (const auto& [received_at, observation] : observations) {
    state_machine_->processSample(observation, received_at);
  }

  const auto effects = state_machine_->tick(ed_->now().seconds());
  if (effects.entered) {
    correlation_id_ = newCorrelationId(robot_id_, "AnalyticsLatencySLO", ed_->now().seconds());
    publishEvent(cloud_native_robotics_interfaces::msg::OperationalEvent::STATE_ENTER, effects);
  } else if (effects.recovered) {
    publishEvent(cloud_native_robotics_interfaces::msg::OperationalEvent::STATE_RECOVERED, effects);
    correlation_id_.clear();
  }
}

void AnalyticsLatencySloRule::publishEvent(std::uint8_t state, const LatencyEffects& effects) {
  using Event = cloud_native_robotics_interfaces::msg::OperationalEvent;
  const auto severity = state == Event::STATE_ENTER ? Event::SEVERITY_WARNING : Event::SEVERITY_INFO;
  event_pub_->publish(makeEvent(
      ed_, robot_id_, "companion-analytics", "AnalyticsLatencySLO", severity, state,
      effects.p95_ms, config_.violation_threshold_ms, config_.window_sec, correlation_id_,
      {
          attribute("recovery_threshold_ms", std::to_string(config_.recovery_threshold_ms)),
          attribute("max_queue_depth", std::to_string(effects.max_queue_depth)),
          attribute("max_cpu_percent", std::to_string(effects.max_cpu_percent)),
          attribute("consecutive_windows", std::to_string(config_.consecutive_windows)),
      }));
}

}  // namespace px4_event_detector_plugin

PLUGINLIB_EXPORT_CLASS(
    px4_event_detector_plugin::AnalyticsLatencySloRule, event_detector::AnalysisRule)
