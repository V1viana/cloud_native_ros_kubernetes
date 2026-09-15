"""Asynchronous deployment update workflow."""

import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from main.models import Deployment, DeploymentEvent, DeploymentJob
from pykuberos.kuberos_executer import KuberosExecuter


logger = logging.getLogger('kuberos.main.tasks')


def flatten_deployment_resources(deployment: Deployment) -> list:
    resources = []
    for job in deployment.deployment_job_set.all():
        resources.extend(job.get_all_rosmodules())
    return resources


def flatten_scheduled_resources(scheduled_jobs: list) -> list:
    resources = []
    for job in scheduled_jobs:
        resources.extend(job.get('sc_onboard', []))
        resources.extend(job.get('sc_edge', []))
    return resources


def _configmap_names(configmaps: list) -> set:
    return {configmap['name'] for configmap in configmaps}


def changed_configmap_names(old_configmaps: list, new_configmaps: list) -> set:
    old_by_name = {configmap['name']: configmap for configmap in old_configmaps}
    new_by_name = {configmap['name']: configmap for configmap in new_configmaps}
    return {
        name for name in old_by_name.keys() | new_by_name.keys()
        if old_by_name.get(name) != new_by_name.get(name)
    }


def _require_success(result: dict, operation: str) -> None:
    if result.get('status') != 'success':
        raise RuntimeError(f'{operation} failed: {result.get("errors", [])}')


def _snapshot_jobs(deployment: Deployment) -> dict:
    return {
        job.robot_name: {
            'disc_server': job.disc_server,
            'ip_reserved': job.ip_reserved,
            'ip_allocated': job.ip_allocated,
        }
        for job in deployment.deployment_job_set.all()
    }


def _rollback_resources(
    executor,
    old_configmaps,
    new_configmaps,
    old_resources,
    new_resources,
    pod_timeout_seconds,
    changed_configmaps,
) -> list:
    errors = []
    try:
        _require_success(
            executor.upsert_configmaps(old_configmaps),
            'ConfigMap rollback',
        )
        _require_success(
            executor.reconcile_rosmodules(
                new_resources,
                old_resources,
                timeout_seconds=pod_timeout_seconds,
                changed_configmaps=sorted(changed_configmaps),
            ),
            'ROS module rollback',
        )
        new_only = _configmap_names(new_configmaps) - _configmap_names(old_configmaps)
        _require_success(
            executor.delete_configmaps_by_name(sorted(new_only)),
            'ConfigMap rollback cleanup',
        )
    except Exception as exc:
        errors.append(str(exc))
    return errors


def _record_failed_update(deployment, event, error, rollback_errors):
    error_message = str(error)
    if rollback_errors:
        error_message += f'; rollback failed: {"; ".join(rollback_errors)}'
        deployment.status = 'failed'
    else:
        deployment.status = 'running'
    deployment.save(update_fields=['status'])
    event.mark_failed(error_message)
    logger.error('Deployment update failed: %s', error_message)
    return {
        'status': 'failed',
        'deployment': deployment.name,
        'revision': deployment.revision,
        'error': error_message,
    }


def _replace_jobs(deployment: Deployment, scheduled_jobs: list, snapshots: dict) -> None:
    deployment.deployment_job_set.all().delete()

    for item in scheduled_jobs:
        previous = snapshots.get(item['robot_name'], {})
        job = DeploymentJob.objects.create(
            robot_name=item['robot_name'],
            job_phase='deploy_success',
            deployment=deployment,
            disc_server=previous.get('disc_server', item.get('sc_disc_server', [])),
            onboard_modules=item.get('sc_onboard', []),
            edge_modules=item.get('sc_edge', []),
            ip_reserved=previous.get('ip_reserved', []),
            ip_allocated=previous.get('ip_allocated', []),
            running_at=timezone.now(),
        )
        job.intialize()
        for pod_status in job.pod_status:
            pod_status['status'] = 'Running'
        for service_status in job.svc_status:
            service_status['status'] = 'Found'
        job.save(update_fields=['pod_status', 'svc_status'])


@shared_task()
def apply_deployment_update(
    deployment_uuid: str,
    event_uuid: str,
    new_manifest: dict,
    new_configmaps: list,
    scheduled_jobs: list,
    pod_timeout_seconds: int = 90,
) -> dict:
    """Apply a replace update while preserving robot discovery resources."""
    deployment = Deployment.objects.get(uuid=deployment_uuid)
    event = DeploymentEvent.objects.get(uuid=event_uuid, deployment=deployment)
    executor = KuberosExecuter(kube_config=deployment.get_main_cluster_config())

    old_configmaps = deployment.get_config_maps() or []
    old_resources = flatten_deployment_resources(deployment)
    new_resources = flatten_scheduled_resources(scheduled_jobs)
    job_snapshots = _snapshot_jobs(deployment)
    changed_configmaps = changed_configmap_names(old_configmaps, new_configmaps)

    try:
        _require_success(
            executor.upsert_configmaps(new_configmaps),
            'ConfigMap reconciliation',
        )
        _require_success(
            executor.reconcile_rosmodules(
                old_resources,
                new_resources,
                timeout_seconds=pod_timeout_seconds,
                changed_configmaps=sorted(changed_configmaps),
            ),
            'ROS module reconciliation',
        )
    except Exception as exc:
        rollback_errors = _rollback_resources(
            executor,
            old_configmaps,
            new_configmaps,
            old_resources,
            new_resources,
            pod_timeout_seconds,
            changed_configmaps,
        )
        return _record_failed_update(deployment, event, exc, rollback_errors)

    try:
        with transaction.atomic():
            deployment = Deployment.objects.select_for_update().get(uuid=deployment_uuid)
            event = DeploymentEvent.objects.select_for_update().get(uuid=event_uuid)
            _replace_jobs(deployment, scheduled_jobs, job_snapshots)
            deployment.deployment_manifest = new_manifest
            deployment.deployment_description = new_manifest
            deployment.config_maps = new_configmaps
            deployment.configmaps_created = True
            deployment.revision = event.target_revision
            deployment.status = 'running'
            deployment.running_at = timezone.now()
            deployment.save(update_fields=[
                'deployment_manifest',
                'deployment_description',
                'config_maps',
                'configmaps_created',
                'revision',
                'status',
                'running_at',
            ])
            event.mark_success()
    except Exception as exc:
        rollback_errors = _rollback_resources(
            executor,
            old_configmaps,
            new_configmaps,
            old_resources,
            new_resources,
            pod_timeout_seconds,
            changed_configmaps,
        )
        deployment = Deployment.objects.get(uuid=deployment_uuid)
        event = DeploymentEvent.objects.get(uuid=event_uuid)
        return _record_failed_update(deployment, event, exc, rollback_errors)

    old_only = _configmap_names(old_configmaps) - _configmap_names(new_configmaps)
    cleanup_result = executor.delete_configmaps_by_name(sorted(old_only))

    return {
        'status': 'success',
        'deployment': deployment.name,
        'revision': deployment.revision,
        'cleanup': cleanup_result,
    }
