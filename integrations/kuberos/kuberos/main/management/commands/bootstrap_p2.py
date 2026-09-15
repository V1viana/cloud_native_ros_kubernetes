"""Bootstrap the isolated P2 KubeROS inventory and API credential."""

import os

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from knox.models import AuthToken
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from main.models import Cluster, ClusterNode, Fleet, FleetNode
from main.tasks.cluster_operating import sync_kubernetes_cluster


SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class Command(BaseCommand):
    help = "Register the dedicated P2 cluster, fleet and robot in KubeROS."

    def handle(self, *args, **options):
        del args, options
        username = os.environ.get("KUBEROS_BOOTSTRAP_USER", "p2-manager")
        password = os.environ.get("KUBEROS_BOOTSTRAP_PASSWORD", "p2-local-only")
        cluster_name = os.environ.get("KUBEROS_CLUSTER_NAME", "cloud-native-p2")
        fleet_name = os.environ.get("KUBEROS_FLEET_NAME", "uav-sim-fleet")
        robot_ids = self._csv(
            os.environ.get("KUBEROS_ROBOT_ID", "drone01")
        )
        onboard_names = self._csv(self._required("KUBEROS_ONBOARD_NODE"))
        if len(robot_ids) != len(onboard_names):
            raise CommandError(
                "KUBEROS_ROBOT_ID and KUBEROS_ONBOARD_NODE must contain "
                "the same number of comma-separated values"
            )
        if len(set(robot_ids)) != len(robot_ids):
            raise CommandError("KUBEROS_ROBOT_ID values must be unique")
        if len(set(onboard_names)) != len(onboard_names):
            raise CommandError("KUBEROS_ONBOARD_NODE values must be unique")
        edge_name = self._required("KUBEROS_EDGE_NODE")
        control_name = os.environ.get("KUBEROS_CONTROL_NODE", "")

        user, _ = get_user_model().objects.get_or_create(username=username)
        user.set_password(password)
        user.is_staff = True
        user.save()

        cluster, _ = Cluster.objects.get_or_create(
            cluster_name=cluster_name,
            defaults={
                "created_by": user,
                "modified_by": user,
                "distribution": Cluster.ClusterDistributionChoices.K3S,
                "env_type": Cluster.EnvTypeChoices.TEST,
                "host_url": "https://kubernetes.default.svc",
                "service_token_admin": "__IN_CLUSTER__",
            },
        )
        cluster.created_by = user
        cluster.modified_by = user
        cluster.host_url = "https://kubernetes.default.svc"
        cluster.service_token_admin = "__IN_CLUSTER__"
        if not cluster.ca_crt_file:
            with open(f"{SERVICE_ACCOUNT_DIR}/ca.crt", "rb") as stream:
                cluster.ca_crt_file.save(
                    "cloud-native-p2-ca.crt",
                    ContentFile(stream.read()),
                    save=False,
                )
        cluster.save()

        result = sync_kubernetes_cluster.run(
            cluster.cluster_config_dict,
            get_pods=False,
        )
        if result and result.get("status") not in {None, "success"}:
            raise CommandError(f"Cluster synchronization failed: {result}")

        onboard_by_robot = {}
        for robot_id, onboard_name in zip(robot_ids, onboard_names):
            onboard_by_robot[robot_id] = self._inventory_node(
                cluster,
                onboard_name,
                "onboard",
                robot_id,
                robot_id,
                "primary",
                False,
            )
        self._inventory_node(cluster, edge_name, "edge", "", "", None, True)
        if control_name:
            self._inventory_node(
                cluster, control_name, "control_plane", "", "", None, False
            )

        fleet, _ = Fleet.objects.get_or_create(
            fleet_name=fleet_name,
            defaults={
                "created_by": user,
                "k8s_main_cluster": cluster,
                "healthy": True,
                "fleet_status": Fleet.FleetStatusChoices.IDLE,
            },
        )
        fleet.created_by = user
        fleet.k8s_main_cluster = cluster
        fleet.healthy = True
        fleet.fleet_status = Fleet.FleetStatusChoices.IDLE
        fleet.save()
        for onboard in onboard_by_robot.values():
            FleetNode.objects.update_or_create(
                fleet=fleet,
                name=onboard.hostname,
                defaults={
                    "cluster_node": onboard,
                    "device_type": "onboard",
                    "shared_resource": False,
                    "status": "deployable",
                },
            )

        AuthToken.objects.filter(user=user).delete()
        _, raw_token = AuthToken.objects.create(user)
        self._publish_token_secret(raw_token)
        self.stdout.write(
            self.style.SUCCESS(
                f"Bootstrapped KubeROS cluster={cluster_name}, "
                f"fleet={fleet_name}, robots={','.join(robot_ids)}"
            )
        )

    @staticmethod
    def _required(name):
        value = os.environ.get(name, "")
        if not value:
            raise CommandError(f"{name} is required")
        return value

    @staticmethod
    def _csv(value):
        items = [item.strip() for item in value.split(",") if item.strip()]
        if not items:
            raise CommandError("Expected at least one comma-separated value")
        return items

    @staticmethod
    def _inventory_node(
        cluster,
        hostname,
        role,
        robot_name,
        robot_id,
        device_group,
        shared,
    ):
        try:
            node = ClusterNode.objects.get(cluster=cluster, hostname=hostname)
        except ClusterNode.DoesNotExist as exc:
            raise CommandError(f"Cluster node '{hostname}' was not discovered") from exc
        node.update_from_inventory_manifest(
            kuberos_role=role,
            robot_name=robot_name,
            robot_id=robot_id,
            onboard_computer_group=device_group,
            shared=shared,
        )
        node.is_label_synced = True
        node.is_alive = True
        node.save(update_fields=["is_label_synced", "is_alive"])
        return node

    @staticmethod
    def _publish_token_secret(raw_token):
        namespace = os.environ.get("POD_NAMESPACE", "cloud-native-p2")
        name = os.environ.get("KUBEROS_TOKEN_SECRET_NAME", "kuberos-api-token")
        config.load_incluster_config()
        api = client.CoreV1Api()
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                labels={"app.kubernetes.io/managed-by": "kuberos-bootstrap"},
            ),
            string_data={"token": raw_token},
            type="Opaque",
        )
        try:
            api.patch_namespaced_secret(name, namespace, body)
        except ApiException as exc:
            if exc.status != 404:
                raise
            api.create_namespaced_secret(namespace, body)
