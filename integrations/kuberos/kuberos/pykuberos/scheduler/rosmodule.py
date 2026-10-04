# Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS (kuberos-io/kuberos commit 0253c9e).
# See THIRD_PARTY_NOTICES (repository root; in the container images: /usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).
# python
import logging
import json

# pykuberos
from .rosparameter import RosParameterList


logger = logging.getLogger('scheduler')


# DEFAULT_DDS_IMAGE_URL = 'metagoto/ros2_dds_server:humble-v1'
# DEFAULT_DDS_IMAGE_URL = 'metagoto/dds_introspection_node:humble-v1.1.1'
# DEFAULT_DDS_IMAGE_URL = 'ros:rolling-ros-base'
DEFAULT_DDS_IMAGE_URL = 'ros:humble-ros-base'
DEFAULT_IMAGE_PULL_SEC = 'kuberos-test-repo'

DEFAULT_ROS_VERSION = 'humble'
DEFAULT_CONTAINER_RESTART_POLICY = 'Never'


def convert_string_to_linux_convention(strings: str) -> str:
    """
    Convert the string to kubernetes naming convention
    """
    return "_".join(strings.split('-')).upper()


class RosModule():
    """
    Basis deployable unit in KubeROS
    Each rosmodule contains serveral cohesive ros packages (nodes) to perform a certain task.
    """

    def __init__(self,
                 name: str,  # robot_name-module_name
                 discovery_svc_name: str,

                 container_image: dict,  # from deployment request
                 entrypoint: list,
                 source_ws: str = None,

                 volumes: list = None,

                 target: str = None,  # target node name or resource group
                 resource_group='edge',  # edge resource group

                 requested_resources: dict = None,  # for batch job

                 privileged: bool = False,
                 advances: dict = None,  # for create nodeport, mount device folder

                 image_pull_secret: str = None,
                 image_pull_policy: str = 'IfNotPresent',  # Always|IfNotPresent|Never
                 node_selector_type: str = 'node',  # node|resource_group
                 ros_version: str = DEFAULT_ROS_VERSION,
                 restart_policy: str = DEFAULT_CONTAINER_RESTART_POLICY,
                 workload_kind: str = 'Pod',
                 replicas: int = 1,
                 probes: dict = None,
                 rmw_implementation: str = 'rmw_fastrtps_cpp',
                 discovery_server_port: int = 11811,
                 source_ros_setup: bool = True,
                 ) -> None:
        """
        RosModule instance for scheduler:

        Args:
            description: rosModule description in KubeROS manifest.
            discovery_svc_name: name of the discovery server service!
        """
        self._name = name
        self.pod_name = None
        self._ros_version = ros_version
        self.discovery_svc_name = discovery_svc_name
        self.node_selector_type = node_selector_type

        self.target = target
        self.resource_group = resource_group

        self.image_name = container_image['image_name']
        self.image_url = container_image['image_url']
        self.entrypoint = entrypoint

        self._source_ws = source_ws

        if requested_resources is not None:
            self._resources = requested_resources
        else:
            self._resources = {}

        self.image_pull_secret = DEFAULT_IMAGE_PULL_SEC if image_pull_secret is None else image_pull_secret
        self.ros_version = ros_version
        self.restart_policy = restart_policy
        self.workload_kind = workload_kind
        self.replicas = replicas
        self.probes = probes or {}
        self._image_pull_policy = image_pull_policy

        self.rmw_implementation = self._normalize_rmw_implementation(rmw_implementation)
        self.discovery_server_port = discovery_server_port
        self.source_ros_setup = bool(source_ros_setup)

        self.volumes = []
        self.volume_mounts = []

        if volumes is not None:
            for volume in volumes:
                self.volumes.append(volume['volume'])
                self.volume_mounts.append(volume['volume_mount'])

        self._advances = advances

        if not privileged:
            self._security_context = {
                'allowPrivilegeEscalation': False,
                'privileged': False
            }
        else:
            self._security_context = {
                'privileged': True
            }
            logger.warning("Container %s is running in privileged mode, be careful!!!", self._name)

        self.env = []
        self.ros_launch_args = []

    @staticmethod
    def _normalize_rmw_implementation(rmw_impl: str) -> str:
        """
        Normalize shorthand names used in manifests.
        """
        if not rmw_impl:
            return 'rmw_fastrtps_cpp'

        mapping = {
            'fastdds': 'rmw_fastrtps_cpp',
            'fastrtps': 'rmw_fastrtps_cpp',
        }

        return mapping.get(rmw_impl, rmw_impl)

    @staticmethod
    def _render_launch_arg(arg_name: str, arg_value) -> str:
        """
        Render a ros2 launch argument safely.
        Use double quotes so shell variables like ${VAR} still expand,
        and values with spaces remain a single launch argument.
        """
        value = str(arg_value)
        value = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'{arg_name}:="{value}"'

    @property
    def pod_manifest(self):
        """
        Get the pod manifest for kubernetes
        TODO: add multiple node selector to prevent the conflict between the nodes.
              and the sync error between KubeROS DB and K8s etcd.
        """
        if self.node_selector_type == 'node':
            node_sel_key = 'device.kuberos.io/hostname'
            node_sel_value = self.target
        else:
            node_sel_key = 'kuberos.io/role'
            node_sel_value = self.resource_group

        launch_args_str = ' '.join([
            self._render_launch_arg(arg['arg_name'], arg['arg_value'])
            for arg in self.ros_launch_args
        ])

        entrypoint = self.entrypoint[0]
        if launch_args_str:
            entrypoint = f'{entrypoint} {launch_args_str}'

        self.args = []

        if self.source_ros_setup:
            self.args.append(f'source /opt/ros/{self._ros_version}/setup.bash')

        if self._source_ws:
            self.args.append(f'source {self._source_ws}setup.bash')

        if self.rmw_implementation:
            self.args.append(f'export RMW_IMPLEMENTATION={self.rmw_implementation}')

        if (
            self.discovery_svc_name
            and self.rmw_implementation in ('rmw_fastrtps_cpp', 'rmw_fastrtps_dynamic_cpp')
        ):
            self.args.append(
                f'export ROS_DISCOVERY_SERVER={self.discovery_svc_name}:{self.discovery_server_port}'
            )

        self.args.append(entrypoint)

        container_spec = {
            'image': self.image_url,
            'name': self.image_name,
            'imagePullPolicy': self._image_pull_policy,
            'command': ["/bin/bash"],
            'args': ['-c', ';'.join(self.args)],
            'ports': [{
                'containerPort': 11811,
                'protocol': 'UDP'
            }],
            'env': self.env,
            'volumeMounts': self.volume_mounts,
            'resources': self._resources,
            'securityContext': self._security_context,
        }
        container_spec.update(self.probes)

        pod_spec = {
            'nodeSelector': {
                node_sel_key: node_sel_value
            },
            'containers': [container_spec],
            'volumes': self.volumes,
            'restartPolicy': (
                'Always' if self.workload_kind == 'Deployment'
                else self.restart_policy
            ),
        }

        if self.image_pull_secret:
            pod_spec['imagePullSecrets'] = [
                {
                    'name': self.image_pull_secret
                }
            ]

        pod_labels = {
            'pod-name': self._name,
            'app.kubernetes.io/name': self._name,
            'app.kubernetes.io/managed-by': 'kuberos',
        }
        self._pod_manifest = {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {
                'name': self._name,
                'labels': pod_labels,
            },
            'spec': pod_spec
        }

        if self.workload_kind == 'Deployment':
            self._pod_manifest = {
                'apiVersion': 'apps/v1',
                'kind': 'Deployment',
                'metadata': {
                    'name': self._name,
                    'labels': pod_labels,
                },
                'spec': {
                    'replicas': self.replicas,
                    # Kubernetes' own default (600s, unset here before) marks
                    # a Deployment's rollout "ProgressDeadlineExceeded" if its
                    # Pod isn't Ready in time -- found live at S3's N=20 that
                    # a startup probe's own restart-and-retry cycle (up to
                    # ~400s under this host's CPU contention at that scale)
                    # can occasionally exceed 600s even though the Pod comes
                    # up healthy shortly after: Kubernetes marks the
                    # Deployment permanently Failed regardless, and KubeROS's
                    # own DeploymentJob model follows that verdict
                    # (models/deployments.py's update_entire_deployment_status,
                    # on job_phase == 'deploy_failed') even though nothing
                    # was actually broken. Raised for headroom; harmless for
                    # E0/P2/S4's own 3-robot fleets, which never approach 600s
                    # to begin with.
                    'progressDeadlineSeconds': 1200,
                    'selector': {
                        'matchLabels': {
                            'pod-name': self._name,
                        }
                    },
                    'strategy': {
                        'type': 'RollingUpdate',
                        'rollingUpdate': {
                            # ROS nodes have a stable graph identity. Avoid
                            # overlapping replicas with the same node name.
                            'maxUnavailable': 1,
                            'maxSurge': 0,
                        }
                    },
                    'template': {
                        'metadata': {
                            'labels': pod_labels,
                        },
                        'spec': pod_spec,
                    },
                },
            }
        return self._pod_manifest

    @property
    def svc_manifests(self):
        self._svc_manifests = []

        if not self._advances:
            return []

        for item in self._advances:
            if item['type'] != 'nodePort':
                continue

            svc = {
                'apiVersion': 'v1',
                'kind': 'Service',
                'metadata': {
                    'name': item['name'] + self._name,
                    'labels': {'svc-name': item['name'] + self._name}
                },
                'spec': {
                    'type': 'NodePort',
                    'selector': {'pod-name': self._name},
                    'ports': [{
                        'port': item['containerPort'],
                        'targetPort': item['containerPort'],
                        'nodePort': item['hostPort']
                    }]
                }
            }
            self._svc_manifests.append(svc)

        return self._svc_manifests

    def set_resources_request_limit(self,
                                    requests: dict,
                                    limits: dict) -> None:
        """
        For using Nvidia GPU:
        resources:
           limits:
             nvidia.com/gpu: 1
        """
        self._resources = {
            'requests': requests,
            'limits': limits
        }

    def get_kubernetes_manifest(self):
        """
        Return the kubernetes pod manifests for deploying the ros module to the target nodes.
        """
        return self.pod_manifest

    def attach_configmap_yaml(self,
                              configmap_name: str,
                              mount_path: str) -> None:

        volume_name = f'{configmap_name}-volume'.replace('.', '-')
        self.volumes.append({
            'name': volume_name,
            'configMap': {
                'name': configmap_name
            }
        })
        self.volume_mounts.append({
            'name': volume_name,
            'mountPath': mount_path,
            'readOnly': True,
        })

    def attach_configmap_key_value(self,
                                   configmap: dict,
                                   launch_param_list):
        """
        Attach a configmap to get the args for the container entrypoint
        TODO: Check the wether the namespace match the configmap name!
        """

        logger.debug(
            "[Scheduling - Rosmodule] Parsing the values from key-value pair RosParam to the container")

        launch_param_dict = {item['param']: item for item in launch_param_list}

        for key, val in configmap['content'].items():

            if key in launch_param_dict:
                arg_name_in_configmap = '{}_{}'.format(
                    configmap['name'].upper().replace('-', '_').replace('.', '_'),
                    launch_param_dict[key]['key'].upper().replace('-', '_').replace('.', '_')
                )

                self.env.append({
                    'name': arg_name_in_configmap,
                    'valueFrom': {
                        'configMapKeyRef': {
                            'name': configmap['name'],
                            'key': launch_param_dict[key]['key']
                        }
                    }
                })

                self.ros_launch_args.append({
                    'arg_name': launch_param_dict[key]['param'],
                    'arg_value': f'${{{arg_name_in_configmap}}}'
                })

            else:
                env_name = key.upper().replace('-', '_').replace('.', '_')

                self.env.append({
                    'name': env_name,
                    'valueFrom': {
                        'configMapKeyRef': {
                            'name': configmap['name'],
                            'key': key
                        }
                    }
                })

    def insert_device_params(self,
                             launch_dev_param_list: list,
                             onboard_node_state: dict):

        for dev_param in launch_dev_param_list:
            dev_name = dev_param['namespace']
            value = self.find_device_params(
                dev_name=dev_name,
                val_key=dev_param['param'],
                peripheral_devices=onboard_node_state['cluster_node_state']['peripheral_devices']
            )
            self.ros_launch_args.append({
                'arg_name': dev_param['param'],
                'arg_value': value
            })

    @staticmethod
    def find_device_params(dev_name: str,
                           val_key: str,
                           peripheral_devices: list) -> str:
        """
        TODO: Correct the device name in FleetState and use NodeState instead of dict.
        """
        print("Device name: {}".format(dev_name))
        print("Peripheral devices: {}".format(peripheral_devices))
        dev_name = dev_name.lower().replace('_', '-')
        val_key = val_key.lower().replace('_', '-')

        for dev in peripheral_devices:
            if dev['deviceName'] == dev_name:
                return dev['parameter'][val_key]
        return ''

    def attach_bridge_server(self):
        """
        Add bridge server
        """
        pass

    def create_image_proxy(self):
        pass

    @property
    def name(self):
        return self._name

    def print_pod_svc(self):
        pod = self.pod_manifest
        print(json.dumps(pod, indent=4, sort_keys=False))


class DiscoveryServer(object):
    """
    Discovery server object for the fleet node.
     - by default, the discovery server is deployed to the robot's main onboard computer.
     - if the robot has multiple onboard computers, a backup discovery server can be
       used to ensure the availability of the discovery server.
    """

    def __init__(self,
                 name: str,
                 port: int,
                 target_node: str = None,  # hostname
                 image_pull_secret: str = None,
                 image_url: str = None,
                 image_pull_policy: str = 'IfNotPresent',
                 requested_resources: dict = None,
                 add_env_for_introspection: bool = False,
                 skip_running: bool = False,
                 volumes: list = None,
                 server_id: int = 0,
                 ) -> None:
        self._name = name
        self.port = port
        self.server_id = int(server_id)

        self._entrypoint = None
        self.kuberos_role = self.name

        self.svc_name = f'{self._name}'
        self.pod_name = self._name

        self.image_url = DEFAULT_DDS_IMAGE_URL if image_url is None else image_url
        self.image_pull_secret = DEFAULT_IMAGE_PULL_SEC if image_pull_secret is None else image_pull_secret
        self.image_pull_policy = image_pull_policy

        self.target_node = target_node
        self.target_port = port
        self.port_protocol = 'UDP'

        if requested_resources is not None:
            self._resources = requested_resources
        else:
            self._resources = {}

        self.volumes = []
        self.volume_mounts = []

        if volumes is not None:
            for volume in volumes:
                self.volumes.append(volume['volume'])
                self.volume_mounts.append(volume['volume_mount'])

        self.env = []
        if add_env_for_introspection:
            self.set_env_for_introsprection()

        self._command = (
            f'source /opt/ros/{DEFAULT_ROS_VERSION}/setup.bash; '
            f'fastdds discovery -i {self.server_id} -l 0.0.0.0 -p {self.target_port}'
        )

        if skip_running:
            self._command = 'sleep 36000'

    def set_env_for_introsprection(self):
        """
        Set the environment variables for introspection.
        """
        self.env.append({
            'name': 'FASTRTPS_DEFAULT_PROFILES_FILE',
            'value': '/dds_super_client_config.xml'
        })

    def set_target_node(self, node_name: str):
        self.target_node = node_name

    @property
    def name(self):
        """
        Return pod name.
        """
        return self._name

    @property
    def discovery_svc_name(self):
        """
        Return service name for rosmodule to connect to the discovery server.
        """
        return self.svc_name

    @property
    def pod_manifest(self):
        """
        Return the pod manifest for kubernetes.
        """
        pod_spec = {
            'nodeSelector': {'device.kuberos.io/hostname': self.target_node},
            'containers': [{
                'image': self.image_url,
                'name': 'dds-discovery-server',
                'imagePullPolicy': self.image_pull_policy,
                'command': ['/bin/bash'],
                'args': ['-c', self._command],
                'ports': [{
                    'containerPort': self.target_port,
                    'protocol': self.port_protocol
                }],
                'resources': self._resources,
                'env': self.env,
                'volumeMounts': self.volume_mounts,
            }],
            'volumes': self.volumes,
            'restartPolicy': 'Always',
        }

        if self.image_pull_secret:
            pod_spec['imagePullSecrets'] = [
                {
                    'name': self.image_pull_secret
                }
            ]

        self._pod_manifest = {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {
                'name': self.pod_name,
                'labels': {
                    "kuberos-robot": self.name,
                    "kuberos-role": 'discovery-server'
                }
            },
            'spec': pod_spec
        }
        return self._pod_manifest

    @property
    def service_manifest(self):
        """
        Return the service manifest for kubernetes.
        """
        self._svc_manifest = {
            'apiVersion': 'v1',
            'kind': 'Service',
            'metadata': {
                'name': self.svc_name,
            },
            'spec': {
                'type': 'ClusterIP',
                'ports': [{
                    'port': self.port,
                    'targetPort': self.target_port,
                    'protocol': self.port_protocol
                }],
                'selector': {
                    "kuberos-robot": self.name,
                    "kuberos-role": 'discovery-server'
                }
            }
        }
        return self._svc_manifest

    def use_custom_image(self,
                         image_url: str,
                         image_pull_secret: str,
                         image_pull_policy: str = 'IfNotPresent') -> None:
        self.image_url = image_url
        self.image_pull_secret = image_pull_secret
        self.image_pull_policy = image_pull_policy

    def set_as_backup_server(self):
        """
        Set the discovery server id as 1.
        """
        pass

    def set_as_primary_server(self):
        """
        Set the discovery server id as 0.
        """
        pass

    def mount_backup_volume(self):
        """
        Mount a volume to cache the dds participant data.
        """
        pass
