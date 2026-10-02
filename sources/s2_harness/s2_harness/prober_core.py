"""S2 health prober core (R11, contract s2-partition-v1, "Raccolta"): no ROS import.

A persistent GetHealthSnapshot client per target, on a node other than
drone01's: each target on its own schedule (nominally every 5 s), at most one
call in flight, a 3 s timeout, so the isolated target never holds up the others.
Outcomes kept apart:
  positive     an answer, healthy, lifecycle 'active', and the expected robot and
               instance (onboard or edge: the S1 classifier accepted only onboard);
  negative     an answer that is unhealthy, not active, or from another identity;
  rpc_timeout  no answer within the timeout (the call is withdrawn);
  error        the collector itself failed (the call could not be made or read).
One JSON line per outcome, with both clocks at request and at outcome.
Tested offline: operator/tests/test_s2_prober.py.
"""

PERIOD_SEC, TIMEOUT_SEC = 5.0, 3.0


def targets(robots=("drone01", "drone02", "drone03"), edge_robots=("drone01",)):
    out = [{"name": f"{r}-onboard", "robot": r, "instance": "onboard", "service": f"/{r}/companion/onboard/health"}
           for r in robots]
    out += [{"name": f"{r}-edge", "robot": r, "instance": "edge", "service": f"/{r}/companion/edge/health"}
            for r in edge_robots]
    return out


def classify(target, response):
    """positive | negative, with the reason and the answer's own fields."""
    fields = {"healthy": bool(response.healthy), "lifecycle_state": response.lifecycle_state,
              "robot_id": response.robot_id, "instance_id": response.instance_id,
              "component": response.component, "detail": response.detail}
    if (response.robot_id, response.instance_id) != (target["robot"], target["instance"]):
        return "negative", "another identity answered", fields
    if not response.healthy:
        return "negative", "unhealthy", fields
    if response.lifecycle_state != "active":
        return "negative", f"lifecycle {response.lifecycle_state}", fields
    return "positive", "", fields


class Prober:
    """port.send(target) -> token; port.poll(token) -> (done, response, error);
    port.cancel(token). tick(now_mono, now_utc) drives everything."""

    def __init__(self, port, log, target_list, *, period_sec=PERIOD_SEC, timeout_sec=TIMEOUT_SEC):
        self._port, self._log = port, log
        self._targets = {t["name"]: t for t in target_list}
        self._period, self._timeout = period_sec, timeout_sec
        self._next = {}
        self._flight = {}             # name -> (token, sent_mono, sent_utc, seq)
        self._seq = 0

    def _record(self, target, flight, outcome, reason, now_mono, now_utc, fields=None):
        _, sent_mono, sent_utc, seq = flight
        self._log.write("health", target=target["name"], robot=target["robot"], instance=target["instance"],
                        service=target["service"], seq=seq, sent_mono=sent_mono, sent_utc=sent_utc,
                        outcome_mono=now_mono, outcome_utc=now_utc, result=outcome, reason=reason,
                        answer=fields)

    def tick(self, now_mono, now_utc):
        for name, target in self._targets.items():
            flight = self._flight.get(name)
            if flight is not None:
                try:
                    done, response, error = self._port.poll(flight[0])
                except Exception as exc:  # noqa: BLE001
                    done, response, error = True, None, exc
                if done:
                    del self._flight[name]
                    if error is not None:
                        self._record(target, flight, "error", f"{type(error).__name__}: {error}", now_mono, now_utc)
                    else:
                        try:
                            result, reason, fields = classify(target, response)
                        except Exception as exc:  # noqa: BLE001
                            result, reason, fields = "error", f"unreadable answer: {exc}", None
                        self._record(target, flight, result, reason, now_mono, now_utc, fields)
                elif now_mono - flight[1] >= self._timeout:
                    del self._flight[name]
                    try:
                        self._port.cancel(flight[0])
                    except Exception:  # noqa: BLE001 -- the timeout is what is recorded
                        pass
                    self._record(target, flight, "rpc_timeout", f"no answer within {self._timeout}s",
                                 now_mono, now_utc)
            due = self._next.setdefault(name, now_mono)
            if name not in self._flight and now_mono >= due:
                self._seq += 1
                try:
                    token = self._port.send(target)
                except Exception as exc:  # noqa: BLE001
                    self._record(target, (None, now_mono, now_utc, self._seq), "error",
                                 f"cannot send: {type(exc).__name__}: {exc}", now_mono, now_utc)
                    token = None
                if token is not None:
                    self._flight[name] = (token, now_mono, now_utc, self._seq)
                while self._next[name] <= now_mono:          # stay on the grid; a late call is not repeated
                    self._next[name] += self._period
