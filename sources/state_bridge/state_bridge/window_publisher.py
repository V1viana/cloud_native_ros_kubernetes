"""Sends the metric-window status off the window timer (R11, decision D6).

Closing a window must never wait for the network: with a link that drops
packets, a PATCH made on the window timer's thread stopped the timer until it
returned, and W = 2 s windows stretched to the length of the wait
(operator/tests/test_window_transport.py). The timer now only offers the
snapshot; one sender thread makes one request at a time.

Only the latest snapshot waits: an offer replaces the pending one, never
queues behind it, and its history (the last window_history windows) already
contains the replaced one's. A failed request is not retried: as before D6, the
next window's snapshot carries the same windows. No outbox, no longer history
(docs/R11_S2_PARTITION.md, D6). No ROS import.
"""

import threading

from .k8s_status_client import K8sStatusClientError


class LatestSnapshotPublisher:
    def __init__(self, send, logger):
        """send(snapshot): the blocking request, raising K8sStatusClientError."""
        self._send = send
        self._logger = logger
        self._condition = threading.Condition()
        self._pending = None
        self._stopped = False
        self._thread = None
        self.offered = self.replaced = self.sent = self.failed = self.errors = 0

    def offer(self, snapshot):
        """Never waits for the network; replaces a snapshot not yet sent."""
        with self._condition:
            if self._pending is not None:
                self.replaced += 1
            self._pending = snapshot
            self.offered += 1
            self._condition.notify()

    def send_pending(self):
        """Send the pending snapshot, if any, on the caller's thread: one step of
        the sender's loop. True if a request was made."""
        with self._condition:
            snapshot, self._pending = self._pending, None
        if snapshot is None:
            return False
        try:
            self._send(snapshot)
            self.sent += 1
        except K8sStatusClientError as exc:
            self.failed += 1
            self._logger.warning(f"cannot publish metric windows: {exc}")
        return True

    def start(self):
        self._thread = threading.Thread(target=self._run, name="metric-windows", daemon=True)
        self._thread.start()

    def stop(self, timeout=None):
        with self._condition:
            self._stopped = True
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
            try:
                self.send_pending()
            except Exception as exc:  # noqa: BLE001 -- see below
                # On the timer, before D6, an unexpected error stopped the node
                # visibly; here it would end this thread and the publication in
                # silence. Logged and counted; the next snapshot is still sent.
                self.errors += 1
                self._logger.error(f"metric windows sender: unexpected {type(exc).__name__}: {exc}")
