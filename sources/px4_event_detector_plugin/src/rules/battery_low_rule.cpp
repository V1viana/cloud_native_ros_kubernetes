#include "px4_event_detector_plugin/rules/battery_low_rule.hpp"

#include <cstdint>
#include <utility>
#include <vector>

#include <event_detector/EventDetector.hpp>
#include <pluginlib/class_list_macros.hpp>

#include "px4_event_detector_plugin/event_support.hpp"

namespace px4_event_detector_plugin {
namespace {

AckStatus ackStatus(const px4_msgs::msg::VehicleCommandAck& ack) {
  if (ack.result == px4_msgs::msg::VehicleCommandAck::VEHICLE_CMD_RESULT_ACCEPTED) {
    return AckStatus::kAccepted;
  }
  if (ack.result == px4_msgs::msg::VehicleCommandAck::VEHICLE_CMD_RESULT_IN_PROGRESS) {
    return AckStatus::kInProgress;
  }
  if (ack.result == px4_msgs::msg::VehicleCommandAck::VEHICLE_CMD_RESULT_TEMPORARILY_REJECTED) {
    return AckStatus::kRetryable;
  }
  return AckStatus::kRejected;
}

}  // namespace

std::string BatteryLowRule::getRuleName() const {
  return "px4_event_detector_plugin::BatteryLowRule";
}

void BatteryLowRule::onInitialize() {}

void BatteryLowRule::loadRuleParameters() {
  std::int64_t consecutive_samples = 3;
  std::int64_t max_attempts = 3;
  std::int64_t target_system = 1;
  std::int64_t target_component = 1;
  std::int64_t source_system = 1;
  std::int64_t source_component = 1;

  loadRuleParameter("robot_id", robot_id_, std::string("drone01"));
  loadRuleParameter("battery_topic", battery_topic_, std::string("/fmu/out/battery_status_v1"));
  loadRuleParameter("command_topic", command_topic_, std::string("/fmu/in/vehicle_command"));
  loadRuleParameter("ack_topic", ack_topic_, std::string("/fmu/out/vehicle_command_ack"));
  loadRuleParameter("vehicle_status_topic", vehicle_status_topic_, std::string("/fmu/out/vehicle_status_v4"));
  loadRuleParameter("event_topic", event_topic_, std::string("/fleet/operational_events"));
  loadRuleParameter("threshold", config_.threshold, 0.20);
  loadRuleParameter("reset_threshold", config_.reset_threshold, 0.25);
  loadRuleParameter("consecutive_samples", consecutive_samples, std::int64_t{3});
  loadRuleParameter("max_message_age_sec", config_.max_message_age_sec, 2.0);
  loadRuleParameter("max_attempts", max_attempts, std::int64_t{3});
  loadRuleParameter("retry_timeout_sec", config_.retry_timeout_sec, 2.0);
  loadRuleParameter("target_system", target_system, std::int64_t{1});
  loadRuleParameter("target_component", target_component, std::int64_t{1});
  loadRuleParameter("source_system", source_system, std::int64_t{1});
  loadRuleParameter("source_component", source_component, std::int64_t{1});

  config_.consecutive_samples = static_cast<std::size_t>(consecutive_samples);
  config_.max_attempts = static_cast<std::size_t>(max_attempts);
  target_system_ = static_cast<int>(target_system);
  target_component_ = static_cast<int>(target_component);
  source_system_ = static_cast<int>(source_system);
  source_component_ = static_cast<int>(source_component);
  state_machine_ = std::make_unique<BatteryStateMachine>(config_);

  rclcpp::QoS event_qos(rclcpp::KeepLast(100));
  event_qos.reliable().transient_local();
  event_pub_ = ed_->create_publisher<cloud_native_robotics_interfaces::msg::OperationalEvent>(
      event_topic_, event_qos);
  command_pub_ = ed_->create_publisher<px4_msgs::msg::VehicleCommand>(
      command_topic_, rclcpp::QoS(10).reliable());

  battery_sub_ = ed_->create_subscription<px4_msgs::msg::BatteryStatus>(
      battery_topic_, rclcpp::SensorDataQoS(),
      [this](px4_msgs::msg::BatteryStatus::ConstSharedPtr message) {
        std::lock_guard<std::mutex> lock(mutex_);
        battery_observations_.push_back(
            {ed_->now().seconds(), message->remaining, message->connected});
      });
  ack_sub_ = ed_->create_subscription<px4_msgs::msg::VehicleCommandAck>(
      ack_topic_, rclcpp::SensorDataQoS(),
      [this](px4_msgs::msg::VehicleCommandAck::ConstSharedPtr message) {
        if (message->command != px4_msgs::msg::VehicleCommand::VEHICLE_CMD_NAV_RETURN_TO_LAUNCH) {
          return;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        acknowledgements_.push_back(ackStatus(*message));
      });
  vehicle_status_sub_ = ed_->create_subscription<px4_msgs::msg::VehicleStatus>(
      vehicle_status_topic_, rclcpp::SensorDataQoS(),
      [this](px4_msgs::msg::VehicleStatus::ConstSharedPtr message) {
        if (message->nav_state != px4_msgs::msg::VehicleStatus::NAVIGATION_STATE_AUTO_RTL) {
          return;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        ++rtl_observations_;
      });
}

void BatteryLowRule::evaluate() {
  const double now_sec = ed_->now().seconds();
  std::deque<BatteryObservation> battery_observations;
  std::deque<AckStatus> acknowledgements;
  std::size_t rtl_observations = 0;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    battery_observations.swap(battery_observations_);
    acknowledgements.swap(acknowledgements_);
    rtl_observations = std::exchange(rtl_observations_, 0);
  }

  for (const auto& observation : battery_observations) {
    latest_remaining_ = observation.remaining;
    handleEffects(
        state_machine_->processSample(
            observation.remaining, observation.connected, now_sec - observation.received_at_sec),
        now_sec);
  }
  for (const auto status : acknowledgements) {
    handleEffects(state_machine_->processAck(status), now_sec);
  }
  for (std::size_t index = 0; index < rtl_observations; ++index) {
    handleEffects(state_machine_->processRtlObserved(), now_sec);
  }
  handleEffects(state_machine_->tick(now_sec), now_sec);
}

void BatteryLowRule::handleEffects(const BatteryEffects& effects, double now_sec) {
  using Event = cloud_native_robotics_interfaces::msg::OperationalEvent;
  if (effects.entered) {
    correlation_id_ = newCorrelationId(robot_id_, "BatteryLow", now_sec);
    publishEvent(Event::STATE_ENTER, Event::SEVERITY_CRITICAL, "threshold_crossed");
  }
  if (effects.send_rtl) {
    sendRtl(now_sec);
  }
  if (effects.acknowledged) {
    publishEvent(Event::STATE_ACTIVE, Event::SEVERITY_WARNING, "rtl_acknowledged");
  }
  if (effects.failed) {
    publishEvent(Event::STATE_FAILED, Event::SEVERITY_CRITICAL, "rtl_not_acknowledged");
  }
  if (effects.recovered) {
    publishEvent(Event::STATE_RECOVERED, Event::SEVERITY_INFO, "battery_recovered");
    correlation_id_.clear();
  }
}

void BatteryLowRule::publishEvent(
    std::uint8_t state, std::uint8_t severity, const std::string& detail) {
  if (correlation_id_.empty()) {
    correlation_id_ = newCorrelationId(robot_id_, "BatteryLow", ed_->now().seconds());
  }
  event_pub_->publish(makeEvent(
      ed_, robot_id_, "px4", "BatteryLow", severity, state, latest_remaining_,
      config_.threshold, 0.0, correlation_id_,
      {
          attribute("detail", detail),
          attribute("rtl_attempts", std::to_string(state_machine_->attempts())),
          attribute("reset_threshold", std::to_string(config_.reset_threshold)),
      }));
}

void BatteryLowRule::sendRtl(double now_sec) {
  px4_msgs::msg::VehicleCommand command;
  command.timestamp = static_cast<std::uint64_t>(now_sec * 1000000.0);
  command.command = px4_msgs::msg::VehicleCommand::VEHICLE_CMD_NAV_RETURN_TO_LAUNCH;
  command.target_system = static_cast<std::uint8_t>(target_system_);
  command.target_component = static_cast<std::uint8_t>(target_component_);
  command.source_system = static_cast<std::uint8_t>(source_system_);
  command.source_component = static_cast<std::uint16_t>(source_component_);
  command.confirmation = static_cast<std::uint8_t>(state_machine_->attempts());
  command.from_external = true;
  command_pub_->publish(command);
  state_machine_->markCommandSent(now_sec);
}

}  // namespace px4_event_detector_plugin

PLUGINLIB_EXPORT_CLASS(
    px4_event_detector_plugin::BatteryLowRule, event_detector::AnalysisRule)
