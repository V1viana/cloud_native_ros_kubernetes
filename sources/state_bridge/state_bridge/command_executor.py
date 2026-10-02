"""State Bridge side of the lifecycle driven by the LifecycleController
(docs/LIFECYCLE_COMMAND_CONTRACT_DRAFT.md, sections 3 and 6). No ROS import.

The controller decides the step, the timeout, the retry budget and the readiness gate
and writes one command per instance in status.lifecycleCommands. The bridge only
executes the authorized step, at most once per command ID, and reports:

  receipt    status.lifecycleReceipts[own key], written by this bridge only:
             Accepted is persisted BEFORE the RPC (resourceVersion precondition on the
             read that validated the command); no persisted Accepted, no RPC;
  outcome    decided by the Accepted-persisted flag set in the callback, not by the
             adapter's bare False (no step, refused callback and a negative answer all
             return False): Succeeded / Failed / Unconfirmed after Accepted; Rejected
             (with its reason) only for a failed check, a callback never called
             (NoApplicableStep) or an adapter error before it (MiddlewareUnavailable),
             and only while the command is still the current one; contention (409)
             or an API error leaves no receipt, for the next tick;
  ID rule    every receipt write or deletion carries a resourceVersion precondition on
             a read that shows the command it refers to is still the relevant one: a
             newer receipt is never overwritten or deleted for an older command.

The MAL is unchanged: set_lifecycle_state(node, toState, before_send, timeout_sec) with
toState adjacent to fromState, and the callback refuses unless the adapter's fresh
state is fromState -- so the adapter can only send the command's own step.
Tested offline: operator/tests/test_lifecycle_commands.py.
"""

import time

from .k8s_status_client import Conflict, K8sStatusClientError
from .ros_middleware_adapter import LifecycleState, MiddlewareUnavailableError

ACCEPTED_ATTEMPTS = 3     # re-read and retry after a 409 on Accepted, then the next tick
WRITE_ATTEMPTS = 3        # the same for an outcome, a Rejected or a cleanup
TERMINAL = ("Succeeded", "Failed", "Unconfirmed")


def own(resource, key):
    status = (resource or {}).get("status") or {}
    return ((status.get("lifecycleCommands") or {}).get(key),
            (status.get("lifecycleReceipts") or {}).get(key))


class CommandExecutor:
    def __init__(self, k8s, mal, *, rosmodule_name, instance_key, pod_uid, readiness, logger,
                 node_name=None, now=None, observe=None):
        self._k8s, self._mal = k8s, mal
        self._name, self._key, self._uid = rosmodule_name, instance_key, pod_uid
        self._node = node_name or instance_key
        self._readiness = readiness          # spec -> True / False / None (no probe)
        self._log = logger
        self._now = now or (lambda: time.time())
        # observe(resource) -> this instance's observation record, taken after the
        # RPC and written in the same patch as the outcome (contract, section 3.6)
        self._observe = observe

    # ---- one tick ----------------------------------------------------------

    def run(self, resource, observed):
        """resource: the ROSModule read this tick; observed: this tick's LifecycleState."""
        self._cleanup(resource)
        command, receipt = own(resource, self._key)
        if not command or command.get("phase") != "Issued":
            return {"action": "none"}
        if receipt and receipt.get("commandId") == command.get("commandId"):
            return {"action": "none", "reason": "receipt exists"}   # never two RPCs for one ID
        reason = self._invalid(command, resource, observed)
        if reason:
            self._reject(command["commandId"], reason)
            return {"action": "rejected", "reason": reason}
        return self._execute(command)

    # ---- checks --------------------------------------------------------------

    def _invalid(self, command, resource, observed):
        spec, meta = resource.get("spec") or {}, resource.get("metadata") or {}
        if command.get("podUID") != self._uid:
            return "PodMismatch"
        if command.get("generation") != meta.get("generation"):
            return "GenerationChanged"
        if command.get("target") != spec.get("lifecycleTarget"):
            return "TargetChanged"
        if self._now() >= float(command.get("deadline", 0)):
            return "Expired"
        if getattr(observed, "value", observed) != command.get("fromState"):
            return "StateMismatch"
        if (command.get("fromState") == "Inactive" and command.get("toState") == "Active"
                and command.get("readinessGate") and self._readiness(spec) is not True):
            return "ReadinessNotPositive"
        return None

    # ---- execution -------------------------------------------------------------

    def _execute(self, command):
        cid = command["commandId"]
        flag = {"accepted": False, "called": False, "reason": None, "accepted_at": None}

        def before_send(current):
            flag["called"] = True
            if getattr(current, "value", current) != command["fromState"]:
                flag["reason"] = "StateMismatch"
                return False
            for _ in range(ACCEPTED_ATTEMPTS):
                try:
                    latest = self._k8s.get_resource(self._name)
                except K8sStatusClientError as exc:
                    self._log.warning(f"command {cid}: cannot re-read before Accepted: {exc}")
                    return False
                now_command, now_receipt = own(latest, self._key)
                if (not now_command or now_command.get("commandId") != cid
                        or now_command.get("phase") != "Issued"
                        or (now_receipt or {}).get("commandId") == cid):
                    return False                     # replaced or already receipted: no receipt
                reason = self._invalid(now_command, latest, current)
                if reason:
                    flag["reason"] = reason
                    return False
                accepted_at = self._now()
                # a whole receipt: nothing of an older one survives the merge patch
                receipt = {"commandId": cid, "podUID": self._uid, "phase": "Accepted",
                           "acceptedAt": accepted_at, "completedAt": None, "reason": None}
                try:
                    result = self._k8s.patch_status(
                        self._name, {"lifecycleReceipts": {self._key: receipt}},
                        resource_version=latest["metadata"]["resourceVersion"])
                except Conflict:
                    continue                         # contention: re-read, check, retry
                except K8sStatusClientError as exc:
                    self._log.warning(f"command {cid}: Accepted not written: {exc}")
                    return False                     # unknown: no RPC, next tick
                stored = own(result, self._key)[1] or {}
                if (stored.get("commandId") != cid or stored.get("phase") != "Accepted"
                        or "completedAt" in stored):
                    self._log.warning(f"command {cid}: Accepted not persisted; update the ROSModule CRD")
                    return False
                flag["accepted"], flag["accepted_at"] = True, accepted_at
                return True
            self._log.info(f"command {cid}: status contention, left for the next tick")
            return False

        timeout = max(0.0, min(float(command["timeoutSec"]), float(command["deadline"]) - self._now()))
        try:
            answered = self._mal.set_lifecycle_state(
                self._node, LifecycleState(command["toState"]), before_send=before_send, timeout_sec=timeout)
        except MiddlewareUnavailableError as exc:
            if flag["accepted"]:
                self._complete(cid, "Unconfirmed", str(exc))
                return {"action": "executed", "outcome": "Unconfirmed"}
            self._reject(cid, "MiddlewareUnavailable")
            return {"action": "rejected", "reason": "MiddlewareUnavailable"}
        if flag["accepted"]:
            outcome = "Succeeded" if answered else "Failed"
            self._complete(cid, outcome)
            return {"action": "executed", "outcome": outcome}
        if not flag["called"]:
            self._reject(cid, "NoApplicableStep")
            return {"action": "rejected", "reason": "NoApplicableStep"}
        if flag["reason"]:
            self._reject(cid, flag["reason"])
            return {"action": "rejected", "reason": flag["reason"]}
        return {"action": "deferred"}

    # ---- receipt writes, each bound to the current command ID ---------------

    def _write(self, cid, build, what):
        """Conditioned write loop: build(latest) -> the status fields, or None when the
        condition no longer holds on that read."""
        for _ in range(WRITE_ATTEMPTS):
            try:
                latest = self._k8s.get_resource(self._name)
            except K8sStatusClientError as exc:
                self._log.warning(f"command {cid}: cannot re-read for {what}: {exc}")
                return False
            fields = build(latest)
            if fields is None:
                return False
            try:
                self._k8s.patch_status(self._name, fields,
                                       resource_version=latest["metadata"]["resourceVersion"])
                return True
            except Conflict:
                continue
            except K8sStatusClientError as exc:
                self._log.warning(f"command {cid}: {what} not written: {exc}")
                return False
        self._log.info(f"command {cid}: {what} left for the next tick (contention)")
        return False

    def _complete(self, cid, phase, detail=None):
        def build(latest):
            receipt = own(latest, self._key)[1]
            if not receipt or receipt.get("commandId") != cid:
                self._log.info(f"command {cid}: outcome {phase} not recorded, a newer receipt exists")
                return None
            done = {**receipt, "phase": phase, "completedAt": self._now()}
            if detail:
                done["reason"] = detail[:200]
            fields = {"lifecycleReceipts": {self._key: done}}
            observation = self._observe(latest) if self._observe else None
            if observation:
                fields["lifecycleInstances"] = {self._key: observation}
            return fields
        return self._write(cid, build, phase)

    def _reject(self, cid, reason):
        def build(latest):
            command, receipt = own(latest, self._key)
            if not command or command.get("commandId") != cid:
                return None                          # replaced: no Rejected for the old command
            if receipt and receipt.get("commandId") == cid:
                return None
            return {"lifecycleReceipts": {self._key: {
                "commandId": cid, "podUID": self._uid, "phase": "Rejected", "reason": reason,
                "completedAt": self._now(), "acceptedAt": None}}}
        self._log.info(f"command {cid}: rejected ({reason})")
        return self._write(cid, build, "Rejected")

    def _cleanup(self, resource):
        """Remove the own receipt once its command is gone or replaced."""
        command, receipt = own(resource, self._key)
        if not receipt or (command and command.get("commandId") == receipt.get("commandId")):
            return
        stale_id = receipt.get("commandId")
        # the first attempt uses the tick's own read, later ones re-read
        for attempt in range(WRITE_ATTEMPTS):
            try:
                latest = resource if attempt == 0 else self._k8s.get_resource(self._name)
            except K8sStatusClientError:
                return
            command, current = own(latest, self._key)
            if not current or current.get("commandId") != stale_id or (
                    command and command.get("commandId") == stale_id):
                return
            try:
                self._k8s.patch_status(self._name, {"lifecycleReceipts": {self._key: None}},
                                       resource_version=latest["metadata"]["resourceVersion"])
                return
            except Conflict:
                continue
            except K8sStatusClientError as exc:
                self._log.warning(f"receipt {stale_id}: cleanup not written: {exc}")
                return
