#include "px4_event_detector_plugin/rule_state_machines.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace px4_event_detector_plugin {

BatteryStateMachine::BatteryStateMachine(BatteryConfig config) : config_(config) {
  if (config_.threshold < 0.0 || config_.reset_threshold > 1.0 ||
      config_.reset_threshold <= config_.threshold || config_.consecutive_samples == 0 ||
      config_.max_attempts == 0 || config_.max_message_age_sec <= 0.0 ||
      config_.retry_timeout_sec <= 0.0) {
    throw std::invalid_argument("invalid battery rule configuration");
  }
}

BatteryEffects BatteryStateMachine::processSample(double remaining, bool connected, double age_sec) {
  BatteryEffects effects;
  const bool valid = connected && std::isfinite(remaining) && remaining >= 0.0 &&
                     remaining <= 1.0 && age_sec >= 0.0 && age_sec <= config_.max_message_age_sec;
  if (!valid) {
    low_samples_ = 0;
    recovery_samples_ = 0;
    return effects;
  }

  if (state_ == State::kNormal) {
    low_samples_ = remaining < config_.threshold ? low_samples_ + 1 : 0;
    if (low_samples_ >= config_.consecutive_samples) {
      state_ = State::kCommandPending;
      low_samples_ = 0;
      effects.entered = true;
      effects.send_rtl = true;
    }
    return effects;
  }

  recovery_samples_ = remaining >= config_.reset_threshold ? recovery_samples_ + 1 : 0;
  if (recovery_samples_ >= config_.consecutive_samples) {
    state_ = State::kNormal;
    recovery_samples_ = 0;
    attempts_ = 0;
    command_accepted_ = false;
    rtl_observed_ = false;
    effects.recovered = true;
  }
  return effects;
}

BatteryEffects BatteryStateMachine::processAck(AckStatus status) {
  BatteryEffects effects;
  if (!incidentActive() || state_ == State::kFailed) {
    return effects;
  }
  if (status == AckStatus::kAccepted) {
    command_accepted_ = true;
    state_ = State::kAcknowledged;
    effects.acknowledged = true;
  } else if (status == AckStatus::kRetryable && attempts_ < config_.max_attempts) {
    effects.send_rtl = true;
  } else if (status == AckStatus::kRejected) {
    state_ = State::kFailed;
    effects.failed = true;
  }
  return effects;
}

BatteryEffects BatteryStateMachine::processRtlObserved() {
  BatteryEffects effects;
  if (incidentActive() && !rtl_observed_) {
    rtl_observed_ = true;
    if (!command_accepted_) {
      state_ = State::kAcknowledged;
      effects.acknowledged = true;
    }
  }
  return effects;
}

BatteryEffects BatteryStateMachine::tick(double now_sec) {
  BatteryEffects effects;
  if (state_ != State::kCommandSent || now_sec - last_command_sec_ < config_.retry_timeout_sec) {
    return effects;
  }
  if (attempts_ < config_.max_attempts) {
    effects.send_rtl = true;
  } else {
    state_ = State::kFailed;
    effects.failed = true;
  }
  return effects;
}

void BatteryStateMachine::markCommandSent(double now_sec) {
  if (!incidentActive() || attempts_ >= config_.max_attempts) {
    return;
  }
  ++attempts_;
  last_command_sec_ = now_sec;
  state_ = State::kCommandSent;
}

bool BatteryStateMachine::incidentActive() const { return state_ != State::kNormal; }

std::size_t BatteryStateMachine::attempts() const { return attempts_; }

HeartbeatStateMachine::HeartbeatStateMachine(HeartbeatConfig config, double started_at_sec)
    : config_(config), started_at_sec_(started_at_sec) {
  if (config_.startup_grace_sec < 0.0 || config_.timeout_sec <= 0.0 ||
      config_.recovery_samples == 0 || config_.recovery_window_sec <= 0.0 ||
      config_.recovery_stability_sec < 0.0 || config_.cooldown_sec < 0.0) {
    throw std::invalid_argument("invalid heartbeat rule configuration");
  }
}

HeartbeatEffects HeartbeatStateMachine::processHeartbeat(double now_sec) {
  HeartbeatEffects effects;
  const bool telemetry_gap =
      !last_heartbeat_sec_ || now_sec - *last_heartbeat_sec_ > config_.timeout_sec;
  last_heartbeat_sec_ = now_sec;
  if (!incident_active_) {
    return effects;
  }

  if (!recovery_started_sec_ || telemetry_gap) {
    recovery_started_sec_ = now_sec;
    recovery_heartbeat_times_.clear();
  }
  recovery_heartbeat_times_.push_back(now_sec);
  while (!recovery_heartbeat_times_.empty() &&
         now_sec - recovery_heartbeat_times_.front() > config_.recovery_window_sec) {
    recovery_heartbeat_times_.pop_front();
  }
  const double stable_for_sec = now_sec - *recovery_started_sec_;
  if (recovery_heartbeat_times_.size() >= config_.recovery_samples &&
      stable_for_sec >= config_.recovery_stability_sec) {
    incident_active_ = false;
    recovery_started_sec_.reset();
    recovery_heartbeat_times_.clear();
    cooldown_until_sec_ = now_sec + config_.cooldown_sec;
    effects.recovered = true;
  }
  return effects;
}

HeartbeatEffects HeartbeatStateMachine::tick(double now_sec) {
  HeartbeatEffects effects;
  if (incident_active_) {
    if (last_heartbeat_sec_ && now_sec - *last_heartbeat_sec_ > config_.timeout_sec) {
      recovery_started_sec_.reset();
      recovery_heartbeat_times_.clear();
    }
    return effects;
  }
  if (!last_heartbeat_sec_ || now_sec < cooldown_until_sec_ ||
      now_sec - started_at_sec_ < config_.startup_grace_sec) {
    return effects;
  }
  if (now_sec - *last_heartbeat_sec_ > config_.timeout_sec) {
    incident_active_ = true;
    recovery_started_sec_.reset();
    recovery_heartbeat_times_.clear();
    effects.entered = true;
  }
  return effects;
}

bool HeartbeatStateMachine::incidentActive() const { return incident_active_; }

LatencySloStateMachine::LatencySloStateMachine(LatencyConfig config) : config_(config) {
  if (config_.recovery_threshold_ms < 0.0 ||
      config_.violation_threshold_ms <= config_.recovery_threshold_ms ||
      config_.window_sec <= 0.0 || config_.consecutive_windows == 0) {
    throw std::invalid_argument("invalid latency SLO rule configuration");
  }
}

void LatencySloStateMachine::processSample(const MetricObservation& sample, double now_sec) {
  if (!std::isfinite(sample.latency_ms) || sample.latency_ms < 0.0 ||
      !std::isfinite(sample.cpu_percent) || sample.cpu_percent < 0.0) {
    return;
  }
  if (!window_started_sec_) {
    window_started_sec_ = now_sec;
  }
  samples_.push_back(sample);
}

LatencyEffects LatencySloStateMachine::tick(double now_sec) {
  LatencyEffects effects;
  if (!window_started_sec_ || now_sec - *window_started_sec_ < config_.window_sec || samples_.empty()) {
    return effects;
  }

  std::vector<double> latencies;
  latencies.reserve(samples_.size());
  for (const auto& sample : samples_) {
    latencies.push_back(sample.latency_ms);
    effects.max_queue_depth = std::max(effects.max_queue_depth, sample.queue_depth);
    effects.max_cpu_percent = std::max(effects.max_cpu_percent, sample.cpu_percent);
  }
  effects.p95_ms = percentile95(std::move(latencies));
  effects.window_completed = true;
  samples_.clear();
  window_started_sec_ = now_sec;

  if (effects.p95_ms > config_.violation_threshold_ms) {
    ++violation_windows_;
    recovery_windows_ = 0;
    if (!incident_active_ && violation_windows_ >= config_.consecutive_windows) {
      incident_active_ = true;
      effects.entered = true;
    }
  } else if (effects.p95_ms < config_.recovery_threshold_ms) {
    violation_windows_ = 0;
    if (incident_active_) {
      ++recovery_windows_;
      if (recovery_windows_ >= config_.consecutive_windows) {
        incident_active_ = false;
        recovery_windows_ = 0;
        effects.recovered = true;
      }
    }
  } else {
    violation_windows_ = 0;
    recovery_windows_ = 0;
  }
  return effects;
}

bool LatencySloStateMachine::incidentActive() const { return incident_active_; }

double LatencySloStateMachine::percentile95(std::vector<double> values) {
  std::sort(values.begin(), values.end());
  const auto rank = static_cast<std::size_t>(std::ceil(0.95 * static_cast<double>(values.size())));
  return values[std::max<std::size_t>(1, rank) - 1];
}

}  // namespace px4_event_detector_plugin
