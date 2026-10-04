# Integration Workspaces

This directory contains project-owned integration copies of external
components. Project development must happen here, not in the original
source workspaces.

## KubeROS

Path:

~~~text
integrations/kuberos
~~~

The directory is a vendored project snapshot. It has two distinct references.

Upstream KubeROS revision (https://github.com/kuberos-io/kuberos, branch `main`)
from which the adaptation started:

~~~text
0253c9ee459145568053bd5c20f96dc1f3eed26c
~~~

Revision of the author's own adaptation repository
(https://github.com/V1viana/kuberos_px4) from which this copy was taken. It is
not an upstream KubeROS revision: it already contains the author's changes.

~~~text
d8ab5294a4cc05d58b529ca360bc1a09d842b107
~~~

It is tracked directly by the parent repository and contains the
project-specific differential deployment update extension.
Any external KubeROS checkout is treated as a read-only baseline and must not
receive project changes.

Check the Django configuration from the integration copy:

~~~bash
cd integrations/kuberos/kuberos
python -B manage.py check
~~~

Run these commands in a Python environment containing the KubeROS Django
dependencies. Virtual environments are local and are not versioned.

## RobotKube Upstreams

The official Event Detector and Application Manager are checked out read-only
in detached HEAD state:

~~~text
integrations/robotkube/event_detector
  32f59d0c2ff1a8be4c48c96f10cee6f0edf6cdbf

integrations/robotkube/application_manager
  8ddb99a70f7f4f571cbaaa0ccea19fb3432424e0
~~~

Both repositories retain their upstream Git history and MIT license. Exact
metadata is recorded in `integrations/robotkube/UPSTREAM_LOCK.yaml`. Project
changes must be implemented as separate plugins or adapter packages until a
deliberate fork strategy is approved.
