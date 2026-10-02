"""CRD group/version shared by every controller, and the resync cadence used
throughout (matches the "Requeue dopo resync period (30s)" step in the
reconciliation workflow diagram in the proposal, S4.1)."""

import os

GROUP = "dronekube.io"
VERSION = "v1alpha1"
RESYNC_INTERVAL_SEC = 30

# AdaptationController alone runs faster than the shared resync period.
# Kopf's @kopf.on.timer only accepts a static float for `interval` -- there
# is no per-object dynamic interval (checked against the installed kopf's
# actual signature before relying on it), so a per-policy spec.trigger.
# windowSec cannot become the tick rate itself. Ticking AdaptationController
# alone faster, and gating the actual window-counting logic on real elapsed
# time against windowSec (see adaptation_controller.py), gets the same
# observed behaviour without raising the poll rate -- and its cost -- of
# the other three controllers, which stay on RESYNC_INTERVAL_SEC.
# 1s, not 5s, since the A/B temporal contract (V6, docs/CRD_CONTRACT_AUDIT.md):
# windows now come closed from the State Bridge and each is counted once, so
# the tick no longer shapes the windows, only how soon a closed window is
# acted on (variant A evaluates every 0.2s). The extra reads and status
# writes are a cost to measure, not to hide.
ADAPTATION_TICK_INTERVAL_SEC = 1

# Same problem, found the same way, in a second controller: LifecycleController
# also used to run on RESYNC_INTERVAL_SEC, so a ROSLifecyclePolicy.spec.
# transitionTimeoutSec shorter than 30s (the proposal's own example value is
# 15s) could never actually be observed -- confirmed live with a 3s policy
# and a ~15-20s real transition: the controller's 30s tick landed only after
# the transition had already settled, so the timeout path never fired even
# though it should have. Same fix as AdaptationController: its own faster
# tick, decoupled from RobotFleetController/ROSModuleController's cost.
LIFECYCLE_TICK_INTERVAL_SEC = 5

# U1/U2-equivalent: how long a rollout has to reach Ready before
# ROSModuleController reverts to the last known-good spec. Comfortably
# above RESYNC_INTERVAL_SEC (30s) on purpose -- unlike windowSec/
# transitionTimeoutSec above, nothing here needs sub-30s resolution, so
# ROSModuleController keeps the shared tick rate rather than getting its
# own faster one.
#
# Started at 60s; found live that a genuinely valid update (U1,
# processing_delay_ms 80.0 -> 95.0) can legitimately take close to that
# long on its own, no fault involved: the Deployment's maxSurge:0/
# maxUnavailable:1 strategy (needed to avoid two ROS 2 nodes with the same
# DDS identity existing at once, see k8s_workloads.py) means the old Pod
# must fully terminate before the new one even starts, and
# companion_analytics is already known to take close to the full default
# termination grace period (~30s) to actually stop (see README.md's
# "Known simplifications"). One live run timed out and rolled back a
# perfectly good update by about one second. 120s leaves real margin
# above that observed ~60s, without being so long that a genuinely broken
# update (U2) takes an impractical amount of wall-clock time to prove.
#
# Overridable via env var, unset (=120) everywhere except S3: found live
# 2026-09-23 that this same check also gates a ROSModule's very first
# Deployment (RollingOut is entered on creation too, not just later
# updates), and at N=20 nearly every one of the 20 robots hit it --
# UpdateRolledBack, "did not converge within 120s" -- once the
# S3-specific startupProbe widening (spec.probes.startup) stopped killing
# Pods prematurely and let them take the time they legitimately needed.
# Same shape of fix as that one: a scenario-scoped escape hatch, not a
# global loosening; a rough estimate given real node/scheduling
# contention at N robots simultaneously, not a measured peak.
ROSMODULE_UPDATE_TIMEOUT_SEC = int(os.environ.get("ROSMODULE_UPDATE_TIMEOUT_SEC", "120"))
