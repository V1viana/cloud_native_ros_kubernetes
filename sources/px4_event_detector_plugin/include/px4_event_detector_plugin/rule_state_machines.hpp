#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <vector>

namespace px4_event_detector_plugin {

enum class AckStatus { kAccepted, kInProgress, kRetryable, kRejected };

struct BatteryEffects {
  bool entered{false};
  bool recovered{false};
  bool failed{false};
  bool send_rtl{false};
  bool acknowledged{false};
};

struct BatteryConfig {
  double threshold{0.20};
  double reset_threshold{0.25};
  std::size_t consecutive_samples{3};
  double max_message_age_sec{2.0};
  std::size_t max_attempts{3};
  double retry_timeout_sec{2.0};
};

class BatteryStateMachine {
 public:
  explicit BatteryStateMachine(BatteryConfig config);

  BatteryEffects processSample(double remaining, bool connected, double age_sec);
  BatteryEffects processAck(AckStatus status);
  BatteryEffects processRtlObserved();
  BatteryEffects tick(double now_sec);
  void markCommandSent(double now_sec);

  bool incidentActive() const;
  std::size_t attempts() const;

 private:
  enum class State { kNormal, kCommandPending, kCommandSent, kAcknowledged, kFailed };

  BatteryConfig config_;
  State state_{State::kNormal};
  std::size_t low_samples_{0};
  std::size_t recovery_samples_{0};
  std::size_t attempts_{0};
  double last_command_sec_{0.0};
  bool command_accepted_{false};
  bool rtl_observed_{false};
};

struct HeartbeatEffects {
  bool entered{false};
  bool recovered{false};
};

struct HeartbeatConfig {
  double startup_grace_sec{60.0};
  double timeout_sec{5.0};
  std::size_t recovery_samples{3};
  double recovery_window_sec{3.0};
  double recovery_stability_sec{3.0};
  double cooldown_sec{60.0};
};

class HeartbeatStateMachine {
 public:
  HeartbeatStateMachine(HeartbeatConfig config, double started_at_sec);

  HeartbeatEffects processHeartbeat(double now_sec);
  HeartbeatEffects tick(double now_sec);
  bool incidentActive() const;

 private:
  HeartbeatConfig config_;
  double started_at_sec_{0.0};
  std::optional<double> last_heartbeat_sec_;
  std::optional<double> recovery_started_sec_;
  std::deque<double> recovery_heartbeat_times_;
  double cooldown_until_sec_{0.0};
  bool incident_active_{false};
};

struct MetricObservation {
  double latency_ms{0.0};
  std::uint32_t queue_depth{0};
  double cpu_percent{0.0};
};

struct LatencyEffects {
  bool entered{false};
  bool recovered{false};
  bool window_completed{false};
  double p95_ms{0.0};
  std::uint32_t max_queue_depth{0};
  double max_cpu_percent{0.0};
};

struct LatencyConfig {
  double violation_threshold_ms{250.0};
  double recovery_threshold_ms{150.0};
  double window_sec{10.0};
  std::size_t consecutive_windows{3};
};

class LatencySloStateMachine {
 public:
  explicit LatencySloStateMachine(LatencyConfig config);

  void processSample(const MetricObservation& sample, double now_sec);
  LatencyEffects tick(double now_sec);
  bool incidentActive() const;

 private:
  static double percentile95(std::vector<double> values);

  LatencyConfig config_;
  std::optional<double> window_started_sec_;
  std::vector<MetricObservation> samples_;
  std::size_t violation_windows_{0};
  std::size_t recovery_windows_{0};
  bool incident_active_{false};
};

}  // namespace px4_event_detector_plugin
