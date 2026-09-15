# Deployment Update API

KubeROS supports an asynchronous replace update for an active ROS deployment.

## Request

~~~http
PATCH /api/v1/deploying/deploy_rosmodule/{deployment_name}/
Authorization: Token {token}
Content-Type: application/json
~~~

The body uses the same input contract as create:

~~~json
{
  "deployment_manifest": {
    "apiVersion": "v1",
    "kind": "ApplicationDeployment",
    "metadata": {
      "name": "demo",
      "targetFleet": "fleet-a",
      "targetRobots": ["robot-1"],
      "rmwImplementation": "fastdds"
    },
    "rosModules": []
  },
  "rosparam_yamls": []
}
~~~

The rosparam_yamls field is optional.

## Contract

Version 1 has the following invariants:

- the deployment must be active and in running state;
- metadata.name, targetFleet, targetRobots and rmwImplementation are immutable;
- Fast DDS is the only supported middleware;
- discovery server Pods and Services are preserved;
- ConfigMaps and module Services are reconciled differentially;
- unchanged module Pods are preserved;
- added, removed or modified module Pods are reconciled selectively;
- a Pod is replaced when it consumes a changed ConfigMap through a volume,
  projected volume, env or envFrom reference;
- concurrent lifecycle operations on the same deployment are rejected.

Because KubeROS currently renders ROS modules as bare Pods, a changed module
can experience a short interruption. Unchanged control modules remain active,
but this is still not a Kubernetes rolling update.

## Accepted Response

~~~json
{
  "status": "accepted",
  "data": {
    "event_id": "3d67c2b0-91ba-4a1a-bdfd-5ad9ac07bd76",
    "target_revision": 2,
    "strategy": "replace"
  },
  "errors": [],
  "msgs": ["Update of deployment <demo> is scheduled."]
}
~~~

HTTP 200 means that the operation was accepted, not that it has completed.

Poll:

~~~http
GET /api/v1/deployment/deployments/{deployment_name}/
~~~

The deployment exposes its last successful revision. Each lifecycle event
exposes event_status, target_revision, timestamps and error_message.

## Failure And Rollback

The Celery task performs these phases:

1. upsert the new ConfigMaps;
2. calculate the Pod, Service and ConfigMap dependency diff;
3. reconcile Services and only the affected module Pods;
4. wait for every created or replaced Pod to reach Running or Succeeded;
5. persist the scheduled resources and increment the revision;
6. remove ConfigMaps no longer referenced.

If phases 1-5 fail, KubeROS attempts to restore the previous ConfigMaps and
module resources. A successful rollback returns the deployment to running
without incrementing its revision. A failed rollback marks it as failed.

Rolling updates and zero-downtime rollback require the planned scheduler
extension from Pod to Deployment.
