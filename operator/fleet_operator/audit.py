"""Kubernetes Event emission + direct audit/notification reporting.

Closes a real gap against the proposal (S4.1/S4.2): "ogni transizione di
riconciliazione eseguita dal Fleet Operator genera un Event Kubernetes
standard" and "Audit Writer ... consuma questi Event (o viene invocato
direttamente dall'Operator)". Until this module, the Fleet Operator did
neither -- confirmed live (`kubectl get events --field-selector
involvedObject.kind=ROSModule` returned nothing after a full E0 run) --
so the declarative variant produced no operator-visible Event trail and no
durable audit record at all, unlike the imperative baseline's own
IncidentReporter (cloud_native_application_manager/incident_reporter.py),
which this module's record shape deliberately mirrors so both variants'
audit.jsonl can be compared with the same tooling.

Direct HTTP invocation, not Event-watching, is the option this module
implements: it is what the imperative baseline already does (Application
Manager posts straight to Audit Writer/Operator Notifier), the proposal
explicitly allows it as an alternative ("o viene invocato direttamente"),
and it avoids building a second, redundant Event-consuming pipeline only
for the declarative side.
"""

import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import kopf

AUDIT_URL = os.environ.get("AUDIT_URL", "")
NOTIFIER_URL = os.environ.get("NOTIFIER_URL", "")


def utc_now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def k8s_event(body, reason, message, type_="Normal", logger=None):
    """Attach a standard Kubernetes Event to the reconciled CR, the same
    operational visibility (`kubectl get events`/`describe`) native
    controllers give Deployment/Job -- proposal S4.2."""
    try:
        kopf.event(body, type=type_, reason=reason, message=message)
    except Exception as exc:  # pragma: no cover - best-effort, never fatal
        if logger:
            logger.warning("could not emit Kubernetes Event %s: %s", reason, exc)


def _post(url, payload, logger):
    if not url:
        return
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2.0) as response:
            response.read()
    except (HTTPError, URLError, OSError) as exc:
        # Best-effort, same posture as Platform Observer/Application
        # Manager's own reporting: audit delivery never blocks or fails
        # reconciliation itself.
        if logger:
            logger.warning("audit delivery to %s failed: %s", url, exc)


# Version of B's audit contract, stamped on every record so that a change in the
# expected records is read as a version change, not as a loss:
#   1  until b692d02: every accepted spec change, lifecycleTarget included, was a
#      revision with a ROSModuleUpdate started/completed pair (e.g. the onboard
#      deactivation of every P2 migration);
#   2  revisions for workload fields only (R1, docs/CRD_CONTRACT_AUDIT.md):
#      lifecycleTarget changes produce no ROSModuleUpdate records;
#   3  an AdaptationPolicy re-arms (R5 residual, R-b): one policy can produce more
#      than one incident, and its correlation_id ends with a random suffix.
AUDIT_CONTRACT_VERSION = 3


def audit(record, logger=None):
    payload = dict(record)
    payload.setdefault("timestamp_utc", utc_now())
    payload.setdefault("audit_contract_version", AUDIT_CONTRACT_VERSION)
    _post(AUDIT_URL, payload, logger)


def notify(notification, logger=None):
    payload = dict(notification)
    payload.setdefault("timestamp_utc", utc_now())
    _post(NOTIFIER_URL, payload, logger)
