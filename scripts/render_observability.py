#!/usr/bin/env python3
"""Render the project's observability components for another namespace (R9, D4b).

Viviana (2026-09-26): E2 gets the Audit Writer and the Operator Notifier in
both variants, reusing the components already in the project rather than new
ones. The single source stays manifests/kubernetes/p2/: 25-observability.yaml
(audit PVC, Audit Writer, Operator Notifier, Platform Observer, the Application
Manager's outbox PVC) and, from 00-rbac.yaml, only the observability
ServiceAccounts, Role and RoleBinding. Rendered for the target namespace, with
the image tag the target cluster imports and the node role it has (E2 is a
single "onboard" node, no "control_plane" one). The outbox PVC is only for a
variant whose Application Manager spools to it (A). YAML on stdout.
Tested offline: operator/tests/test_render_observability.py.
"""

import argparse
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAMESPACE = "cloud-native-p2"
SOURCE_IMAGE = "cloud-native-ros/control-plane:p2"
COMPONENTS = ("p2-audit-writer", "p2-operator-notifier", "p2-platform-observer")
OUTBOX = "p2-manager-outbox"


def _retarget(node, namespace, image, node_role):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "namespace" and value == SOURCE_NAMESPACE:
                value = namespace
            elif key == "image" and value == SOURCE_IMAGE:
                value = image
            elif key == "kuberos.io/role" and value == "control_plane":
                value = node_role
            elif key == "value" and value == SOURCE_NAMESPACE:      # KUBERNETES_NAMESPACE env
                value = namespace
            out[key] = _retarget(value, namespace, image, node_role)
        return out
    if isinstance(node, list):
        return [_retarget(item, namespace, image, node_role) for item in node]
    return node


def render(namespace, image, node_role, with_outbox):
    base = ROOT / "manifests/kubernetes/p2"
    rbac = [d for d in yaml.safe_load_all((base / "00-rbac.yaml").read_text())
            if d and d.get("metadata", {}).get("name") in COMPONENTS]
    components = [d for d in yaml.safe_load_all((base / "25-observability.yaml").read_text()) if d]
    if not with_outbox:
        components = [d for d in components if d["metadata"]["name"] != OUTBOX]
    return [_retarget(d, namespace, image, node_role) for d in rbac + components]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--node-role", required=True)
    parser.add_argument("--with-outbox", action="store_true")
    args = parser.parse_args(argv)
    yaml.safe_dump_all(render(args.namespace, args.image, args.node_role, args.with_outbox),
                       sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
