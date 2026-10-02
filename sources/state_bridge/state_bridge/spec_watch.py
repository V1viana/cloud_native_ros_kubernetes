"""SpecWatcher: the State Bridge's watch on its own ROSModule (no ROS import).

Proposal S4.2: the bridge's Kubernetes client performs "un watch sullo spec
della stessa CR per ricevere i comandi dichiarativi". Until 2026-09-23 the
bridge GET its ROSModule every 5s instead (docs/CRD_CONTRACT_AUDIT.md, R4).

List, then watch from the list's resourceVersion, in a plain thread outside the
ROS executor. The API server closes each watch after watch_timeout_sec; the
list that follows is the periodic resync (30s, the proposal's resync period),
when connected. Socket timeouts and backoff extend this interval on failures.
410 Gone lists again at once; transport failures retry with exponential backoff.

current() hands the core a copy of the resource only while it is trustworthy:
a watch is open, or a list succeeded within stale_after_sec. Otherwise it
returns None and the core reads the resource itself -- during an outage the
bridge degrades to polling. Even an open watch can lag behind writes: the core
guards full lifecycle records with resourceVersion and merges observations
without rewriting the durable retry budget.
"""

import copy
import threading
import time

from .k8s_status_client import K8sStatusClientError, WatchExpired
from .window_trace import NULL_DETAIL


class SpecWatcher:
    # Option 3 of the D6 diagnosis: set by bridge.py to the core's DetailRecorder
    # when the detail trace is on -- lists, events, and the lock around the cache.
    detail = NULL_DETAIL

    def __init__(self, client, name, logger, *, watch_timeout_sec=30,
                 stale_after_sec=45, backoff_sec=(1.0, 30.0), monotonic=time.monotonic):
        self._client = client
        self._name = name
        self._logger = logger
        self._watch_timeout_sec = watch_timeout_sec
        self._stale_after_sec = stale_after_sec
        self._backoff_sec = backoff_sec
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._resource = None
        self._generation = None
        self._changed = False
        self._watching = False
        self._synced_at = None
        self._thread = None
        self.stats = {"lists": 0, "watches": 0, "events": 0, "expired": 0, "errors": 0}

    def start(self):
        self._thread = threading.Thread(target=self.run, name="spec-watch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def current(self):
        """A copy of the resource if the watch is trustworthy right now, else None."""
        timed = self.detail.enabled
        requested = time.monotonic() if timed else None
        with self._lock:
            acquired = time.monotonic() if timed else None
            fresh = self._watching or (
                self._synced_at is not None
                and self._monotonic() - self._synced_at <= self._stale_after_sec)
            resource = copy.deepcopy(self._resource) if fresh else None
        if timed:
            self.detail.item("current", requested, time.monotonic(),
                             lock_wait_us=round((acquired - requested) * 1e6), fresh=fresh)
        return resource

    def take_change(self):
        """True once after each generation change (spec edit) seen by the watch."""
        with self._lock:
            changed, self._changed = self._changed, False
            return changed

    def _store(self, resource, *, synced):
        timed = self.detail.enabled
        requested = time.monotonic() if timed else None
        with self._lock:
            acquired = time.monotonic() if timed else None
            self._resource = resource
            generation = (resource or {}).get("metadata", {}).get("generation")
            if generation != self._generation:
                self._generation = generation
                self._changed = True
            if synced:
                self._synced_at = self._monotonic()
        if timed:
            self.detail.item("store", requested, time.monotonic(), lock_wait_us=round((acquired - requested) * 1e6))

    def _set_watching(self, value):
        with self._lock:
            self._watching = value
            if value:
                self._synced_at = self._monotonic()

    def run(self):
        delay = self._backoff_sec[0]
        while not self._stop.is_set():
            try:
                listed = time.monotonic() if self.detail.enabled else None
                resource, version = self._client.list_resource(self._name)
                if listed is not None:
                    self.detail.item("list", listed, time.monotonic())
                self.stats["lists"] += 1
                self._store(resource, synced=True)
                self.stats["watches"] += 1
                self._set_watching(True)
                for event in self._client.watch_resource(
                        self._name, version, self._watch_timeout_sec):
                    if self._stop.is_set():
                        return
                    self.stats["events"] += 1
                    if self.detail.enabled:
                        meta = (event.get("object") or {}).get("metadata") or {}
                        now = time.monotonic()
                        self.detail.item("watch_event", now, now, type=event.get("type"),
                                         rv=meta.get("resourceVersion"), generation=meta.get("generation"))
                    if event.get("type") in ("ADDED", "MODIFIED"):
                        self._store(event.get("object"), synced=True)
                    elif event.get("type") == "DELETED":
                        self._store(None, synced=True)
                    # BOOKMARK: nothing to keep, the next list resynchronises.
                delay = self._backoff_sec[0]   # clean close by the server: resync now
            except WatchExpired:
                self.stats["expired"] += 1     # list again at once
            except K8sStatusClientError as exc:
                self._set_watching(False)
                self.stats["errors"] += 1
                self._logger.warning(f"spec watch interrupted, retrying in {delay:.0f}s: {exc}")
                self._stop.wait(delay)
                delay = min(delay * 2, self._backoff_sec[1])
            finally:
                self._set_watching(False)
