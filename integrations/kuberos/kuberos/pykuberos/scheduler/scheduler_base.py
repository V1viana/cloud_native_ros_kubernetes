# Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS (kuberos-io/kuberos commit 0253c9e).
# See THIRD_PARTY_NOTICES (repository root; in the container images: /usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).
# python
import logging
from typing import Optional, List

# Pykuberos
from .rosmodule import RosModule, DiscoveryServer
from .manifest import RosModuleManifest
from .rosparameter import RosParamMapList

logger = logging.getLogger('scheduler')


class RobotEntity():
    """
    A robot can have multiple onboard computers.
    For simple deployment, we assume that there is only one onboard computer.
    """

    def __init__(self, node_state) -> None:
        """
        node_state: dict - the node state of the primary node of the robot.
        """

        self.rmw_impl = 'fastdds'
        self._robot_name = node_state['robot_name']
        self.robot_id = node_state['robot_id']
        self.hostname = node_state['hostname']
        self.robot_primary_node_name = node_state['hostname']
        self._node_state = node_state

        self.onboard_module_mani = []  # List of RosModuleManifest
        self.edge_module_mani = []

        self.sc_onboard_modules = []  # list of scheduled RosModule instance
        self.sc_edge_modules = []  # list of scheduled RosModule instance

        self.sc_onboard = []
        self.sc_edge = []

        self.primary_discovery_server = None
        self.pri_disc_svc_name = None
        self.discovery_server_policy = {
            'create': True,
            'serviceName': '',
            'port': 11811,
        }

    def configure_discovery_server(self, policy: dict) -> None:
        self.discovery_server_policy.update(policy or {})

    @staticmethod
    def _normalize_rmw_implementation(rmw_impl: str) -> str:
        """
        Normalize shorthand names used in manifests / scheduler.
        """
        if not rmw_impl:
            return 'fastdds'

        mapping = {
            'fastrtps': 'fastdds',
            'rmw_fastrtps_cpp': 'fastdds',
            'rmw_fastrtps_dynamic_cpp': 'fastdds',
        }

        return mapping.get(rmw_impl, rmw_impl)

    def _uses_fastdds_discovery(self) -> bool:
        """
        Return True only when the selected middleware should use Fast DDS discovery server.
        """
        normalized = self._normalize_rmw_implementation(self.rmw_impl)
        return normalized == 'fastdds'

    def schedule_primary_discovery_server(self) -> list:
        """
        Bind a default discovery server to the primary node.
        """
        if not self._uses_fastdds_discovery():
            # if using CycloneDDS or another middleware, skip the discovery server
            self.primary_discovery_server = None
            self.pri_disc_svc_name = None
            return []

        if not self.discovery_server_policy.get('create', True):
            service_name = self.discovery_server_policy.get('serviceName', '')
            if not service_name:
                raise ValueError(
                    'metadata.discoveryServer.serviceName is required '
                    'when discovery server creation is disabled.'
                )
            self.primary_discovery_server = None
            self.pri_disc_svc_name = (
                self.discovery_server_policy.get('address') or service_name
            )
            return []

        self.primary_discovery_server = DiscoveryServer(
            name=f'{self._robot_name}-primary-discovery-server',
            port=self.discovery_server_policy.get('port', 11811),
            target_node=self.robot_primary_node_name,
            server_id=0,
        )
        self.pri_disc_svc_name = self.primary_discovery_server.discovery_svc_name

        pod_manifest = self.primary_discovery_server.pod_manifest
        svc_manifest = self.primary_discovery_server.service_manifest
        return [{
            'pod': pod_manifest,
            'svc': svc_manifest,
        }]

    def bind_rosmodule(self,
                       module_manifest: RosModuleManifest,
                       rmw_impl: str = 'fastdds') -> None:
        """
        Bind the ros_modules to the robot
        """
        # get target node
        target = module_manifest.preference

        self.rmw_impl = self._normalize_rmw_implementation(rmw_impl)

        if target == 'onboard':
            self.onboard_module_mani.append(module_manifest)
        elif target == 'edge':
            self.edge_module_mani.append(module_manifest)

    def check_onboard_module_validity(self, node_state: dict):
        """
        Check the validity of the onbard module manifest and
        the required resources on the node, such as
         - cpu architecture
         - container runtime
         - mounted peripheral devices: like robot, camera, lidar, gripper, etc.
        """

        err_msgs = []

        # check the required peripheral devices
        node_peri_dev_list = node_state['cluster_node_state']['peripheral_devices']
        for mani in self.onboard_module_mani:
            req_dev_list = mani.peripheral_devices
            for req_dev in req_dev_list:
                if req_dev not in node_peri_dev_list:
                    err_msgs.append(
                        f'Required peripheral device {req_dev} is not available on the node {self.robot_primary_node_name}'
                    )

        # check the cpu architecture

        # check the container runtime

        # check the nvidia gpu

        if len(err_msgs) > 0:
            return False, err_msgs

        logger.info("[Scheduling] Check validity of onboard module [PASSED]")

        return True, ''

    def _attach_required_rosparameter_and_env_var(self,
                                                  scheduled_module: RosModule,
                                                  module_manifest,
                                                  rosparam_maps: RosParamMapList) -> None:
        """
        Attach required ROS parameters to the scheduled module.

        :param scheduled_module: The ROS module scheduled to be attached.
        :param module_manifest: ROS module manifest.
        :param rosparam_maps: List containing ROS parameter maps.
        """

        # Get the required rosparameters from ROSModuleManifest
        req_rosparam_list = module_manifest.get_rosparam_list()
        # Match the corresponding RosParamMap
        # Find the custom rosparam from the RosParamMap
        req_rosparam_list.match_rosparam_map_list(rosparam_maps)

        # Get the required ros2 launch args
        req_launch_param = module_manifest.get_launch_param()
        launch_rosparam_list = module_manifest.get_launch_param_rosparam()

        logger.debug("Required launch param: %s", req_launch_param)

        # Loop to attach parameters from the manifest to the scheduled module
        # Parameters from each RosParamMap are used in
        #   - volume mount - yaml
        #   - environment variables - key-value
        #   - launch parameters - key-value
        for req_rosparam in req_rosparam_list.rosparam_list:

            # get configmap
            configmap = rosparam_maps.get_configmap_by_name(
                param_map_name=req_rosparam.value_from
            )

            logger.debug(
                "[Scheduling] Attaching required rosparam: %s \n - Value from: %s \n - ConfigMap: %s",
                req_rosparam.name,
                req_rosparam.value_from,
                configmap
            )

            if configmap == {}:
                # TODO: raise error
                logger.error(
                    "[Scheduling] RosParam <%s> Configmap is empty",
                    req_rosparam.name
                )

            if req_rosparam.type == 'yaml':
                # attach the configmap to scheduled ros module
                # mount the configmap to the container
                scheduled_module.attach_configmap_yaml(
                    configmap_name=configmap.get('name'),
                    mount_path=req_rosparam.mount_path
                )

            if req_rosparam.type == 'key-value':
                # add ENV variables with valueFrom - configMapKeyRef
                # append the launch parameters with arg_value from the configmap
                # TODO support dynamically setting the ros parameters through configmap
                scheduled_module.attach_configmap_key_value(
                    configmap=configmap,
                    launch_param_list=launch_rosparam_list,
                )

    @staticmethod
    def _build_service_manifest(module_manifest: RosModuleManifest,
                                pod_name: str) -> dict:
        """
        Build a Kubernetes Service for a scheduled ROS module.
        The Service selects the module Pod through the 'pod-name' label
        generated by RosModule.pod_manifest.
        """
        ports = []

        for port in module_manifest.service_ports:
            service_port = {
                'port': port.get('port', 11811),
                'targetPort': port.get('targetPort', port.get('port', 11811)),
                'protocol': port.get('protocol', 'UDP'),
            }

            if port.get('name'):
                service_port['name'] = port.get('name')

            ports.append(service_port)

        return {
            'apiVersion': 'v1',
            'kind': 'Service',
            'metadata': {
                'name': module_manifest.service_name,
                'labels': {
                    'svc-name': module_manifest.service_name,
                }
            },
            'spec': {
                'type': module_manifest.service_type,
                'selector': {
                    'pod-name': pod_name,
                },
                'ports': ports,
            }
        }

    def schedule_onboard_modules(self,
                                 rosparam_maps: RosParamMapList) -> list:
        """
        Schedule the onboard modules to the primary node.
        """
        for module_mani in self.onboard_module_mani:

            # Initialize the pod
            pod_name = module_mani.name if module_mani.is_fleet_scoped else f'{self._robot_name}-{module_mani.name}'
            target_node = self.robot_primary_node_name
            sc_module = RosModule(
                name=pod_name,
                discovery_svc_name=self.pri_disc_svc_name,
                target=target_node,
                node_selector_type='node',
                container_image=module_mani.container_image,
                image_pull_secret=module_mani.container_registry['imagePullSecret'],
                image_pull_policy=module_mani.container_registry['imagePullPolicy'],
                entrypoint=module_mani.entrypoint,
                source_ws=module_mani.source_ws,
                privileged=module_mani.privileged,
                workload_kind=module_mani.workload_kind,
                replicas=module_mani.replicas,
                probes=module_mani.probes,
                requested_resources=module_mani.resources,
                rmw_implementation=self.rmw_impl,
                source_ros_setup=module_mani.source_ros_setup,
            )

            # Attach ros parameters and environment variables
            self._attach_required_rosparameter_and_env_var(
                scheduled_module=sc_module,
                module_manifest=module_mani,
                rosparam_maps=rosparam_maps
            )

            # Add the device parameter from the fleet state
            launch_dev_param_list = module_mani.get_launch_param_device()
            sc_module.insert_device_params(
                launch_dev_param_list=launch_dev_param_list,
                onboard_node_state=self._node_state
            )

            # Add optional Service before the Pod, so Kubernetes service
            # discovery is available as early as possible.
            if module_mani.service_enabled:
                self.sc_onboard.append(
                    self._build_service_manifest(
                        module_manifest=module_mani,
                        pod_name=pod_name
                    )
                )

            # Add the rosmodule to the scheduled list
            self.sc_onboard_modules.append(sc_module)
            self.sc_onboard.append(sc_module.get_kubernetes_manifest())

            logger.debug("[Scheduling] ROS launch args: %s", sc_module.ros_launch_args)
            logger.debug("[Scheduling] Entry point: %s", sc_module.entrypoint)

        return self.sc_onboard

    def schedule_edge_modules(self,
                              rosparam_maps: RosParamMapList):
        """
        After binding the edge modules to the robot,
        check the feasibility of deploying the edge modules to the edge.

        If the requirement is not fulfilled, reschedule
        the edge modules to the onboard computers.
        """
        for module_mani in self.edge_module_mani:

            # Intitialize the pod
            pod_name = module_mani.name if module_mani.is_fleet_scoped else f'{self._robot_name}-{module_mani.name}'
            sc_module = RosModule(
                name=pod_name,
                discovery_svc_name=self.pri_disc_svc_name,
                node_selector_type='edge',
                container_image=module_mani.container_image,
                image_pull_secret=module_mani.container_registry['imagePullSecret'],
                image_pull_policy=module_mani.container_registry['imagePullPolicy'],
                entrypoint=module_mani.entrypoint,
                source_ws=module_mani.source_ws,
                privileged=module_mani.privileged,
                workload_kind=module_mani.workload_kind,
                replicas=module_mani.replicas,
                probes=module_mani.probes,
                requested_resources=module_mani.resources,
                rmw_implementation=self.rmw_impl,
                source_ros_setup=module_mani.source_ros_setup,
            )

            # Attach ros parameters and environment variables
            self._attach_required_rosparameter_and_env_var(
                scheduled_module=sc_module,
                module_manifest=module_mani,
                rosparam_maps=rosparam_maps
            )

            # Add optional Service before the Pod, so Kubernetes service
            # discovery is available as early as possible.
            if module_mani.service_enabled:
                self.sc_edge.append(
                    self._build_service_manifest(
                        module_manifest=module_mani,
                        pod_name=pod_name
                    )
                )

            # Add the rosmodule to the scheduled list
            self.sc_edge_modules.append(sc_module)
            self.sc_edge.append(sc_module.get_kubernetes_manifest())

        return self.sc_edge

    @property
    def onboard_primary_node_name(self):
        """
        Return the hostname of the primary onboard computer.
        """
        return self.hostname

    def get_sc_modules(self):
        """
        Return all kubernetes pod and service manifests of the scheduled modules.
        """
        dds_pod_list = []
        dds_svc_list = []

        if self.primary_discovery_server is not None:
            dds_pod_list = [self.primary_discovery_server.pod_manifest]
            dds_svc_list = [self.primary_discovery_server.service_manifest]

        onboard_pod_list = []
        edge_pod_list = []

        for module in self.sc_onboard_modules:
            onboard_pod_list.append(module.get_kubernetes_manifest())

        for module in self.sc_edge_modules:
            edge_pod_list.append(module.get_kubernetes_manifest())

        return {
            'dds_pod_list': dds_pod_list,
            'dds_svc_list': dds_svc_list,
            'onboard_pod_list': onboard_pod_list,
            'edge_pod_list': edge_pod_list
        }

    def get_discovery_server(self):
        if self.primary_discovery_server is None:
            return {}

        pod_manifest = self.primary_discovery_server.pod_manifest
        svc_manifest = self.primary_discovery_server.service_manifest
        return {
            'pod': pod_manifest,
            'svc': svc_manifest,
        }

    def get_scheduled_modules(self):
        sc_modules_k8s = []
        for module in self.sc_onboard_modules:
            sc_modules_k8s.append(module.get_kubernetes_manifest())
        for module in self.sc_edge_modules:
            sc_modules_k8s.append(module.get_kubernetes_manifest())
        return sc_modules_k8s

    def get_onboard_modules(self):
        return self.onboard_module_mani

    def get_edge_modules(self):
        return self.edge_module_mani

    @property
    def robot_name(self):
        return self._robot_name

    def __repr__(self) -> str:
        return f'RobotName: {self._robot_name}, Onboard: {self.onboard_module_mani}, Edge: {self.edge_module_mani}'


class SchedulingMsgs():
    """
    Gathering the analysis result from the scheduler.
    Add message to the message queue.
    Check result.
    Message types:
     - Debug:
     - Info:
     - Warning:
     - Error:
    """
    def __init__(self, ) -> None:
        self.msgs = []

    def add_msg(self,
                msg_type: str,
                msg: str):
        """
        Add msg with msg type:
        args:
            - msg_type: debug | info | warning | error
        """
        if msg_type not in ['debug', 'info', 'warning', 'error']:
            print("Invalid msg type")
            self.msgs.append({msg_type: msg})

    def contains_error(self):
        """
        Check whether error occured in the scheduling process.
        """
        for msg in self.msgs:
            if 'error' in msg.keys():
                return True
        return False

    def get_msgs(self):
        """
        Get all messages
        """
        return self.msgs

    def print_msgs(self):
        """
        Print messages for debugging
        """
        print(self.msgs)
