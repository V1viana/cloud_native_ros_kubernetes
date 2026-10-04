# Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS (kuberos-io/kuberos commit 0253c9e).
# See THIRD_PARTY_NOTICES (repository root; in the container images: /usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).
# python
from typing import List

# pykuberos
from .rosmodule import RosModule
from .rosparameter import RosParameter, RosParamMap, RosParameterList


WORKSPACE_PATH_DEFAULT = '/workspace/install/'


class DeploymentManifest(object):
    """
    KubeROS deployment manifest.
    """

    def __init__(self,
                 manifest: dict) -> None:

        self._manifest = manifest or {}
        self._rosmodules_mani = []

        for module_mani in self._manifest.get('rosModules', []):
            self._rosmodules_mani.append(
                RosModuleManifest(
                    module_mani,
                    container_registry_list=self.container_registry
                )
            )

    @staticmethod
    def _normalize_rmw_implementation(rmw_impl: str) -> str:
        """
        Normalize middleware names coming from the deployment manifest.
        """
        if not rmw_impl:
            return 'fastdds'

        mapping = {
            'fastrtps': 'fastdds',
            'rmw_fastrtps_cpp': 'fastdds',
            'rmw_fastrtps_dynamic_cpp': 'fastdds',
        }

        return mapping.get(rmw_impl, rmw_impl)

    @property
    def metadata(self) -> dict:
        """
        Return the metadata as dict
        """
        return self._manifest.get('metadata', {})

    @property
    def rmw_implementation(self) -> str:
        return self._normalize_rmw_implementation(
            self.metadata.get('rmwImplementation', 'fastdds')
        )

    @property
    def rosmodules_mani(self) -> List[dict]:
        """
        Return the list of rosmodules manifest
        # TODO: Check it!
        """
        return self._rosmodules_mani

    @property
    def discovery_server(self) -> dict:
        """Return the robot discovery-server policy for this deployment."""
        config = self.metadata.get('discoveryServer', {}) or {}
        return {
            'create': bool(config.get('create', True)),
            'serviceName': config.get('serviceName', ''),
            'address': config.get('address', ''),
            'port': int(config.get('port', 11811)),
        }

    def get_target_robot_names(self):
        """
        Check wether this deployment is for the specific robots.
        Return:
            - list of robot names
            - empty list if this deployment is for all robots in this fleet.
        """
        return self.metadata.get('targetRobots', [])

    @property
    def rosparam_map(self) -> List[RosParamMap]:
        """
        Get the custom ros parameters from the deployment manifest.
        """
        return self._manifest.get('rosParamMap', [])

    @property
    def staticfile_map(self):
        """
        Get the static file maps
        """
        return self._manifest.get('staticFileMap', [])

    def substitute_params(self,
                          hardware_specs: dict,
                          configmap: dict):
        pass

    @property
    def container_registry(self):
        # TODO: Use default or customized
        container_registry = self._manifest.get('containerRegistry', None)
        if not container_registry:
            return []
        return container_registry  # TODO

    def get_default_container_registry(self):
        result = {
            'imagePullSecret': '',
            'imagePullPolicy': 'IfNotPresent'
        }
        container_registry = self._manifest.get('containerRegistry', None)

        if container_registry is None:
            return result

        # find the default container registry
        for item in container_registry:
            if item['name'] == 'default':
                result['imagePullSecret'] = item['imagePullSecretName']
                result['imagePullPolicy'] = item['imagePullPolicy']
                break

        return result

    def __repr__(self) -> str:
        return f'<DeploymentManifest: {self.metadata.get("name", "unknown")}>'


class RosModuleManifest(object):
    """
    Object to parse the RosModule in the deployment manifest.
    Each ros module contains the following information:
        - container image name / address / entrypoint
        - preference: onboard / edge / cloud
        - requirements
        - launchParameters -> Parameters used as arguments for the ros launch file.
            - device sepecific parameters: upper case, e.g. {SIM_ARM.ROBOT_IP},
                                           acquired from KubeROS device registry.
            - launch arguments: lower case, e.g. {launch_parameters.use_sim},
                                           acquired from the attached RosParamMap
        - rosParameters -> loaded from the rosParamMap and are used as ROS paramteters.
        - staticFiles -> loaded from the staticFileMap and are used as static files.
    """

    def __init__(self,
                 rosmodule_manifest: dict,
                 container_registry_list: list) -> None:
        """
        Parse the rosmodule manifest.
        """
        self._module_mani = rosmodule_manifest
        self._requirements = self._module_mani.get('requirements', {})

        self._container_registry = {
            'imagePullSecret': '',
            'imagePullPolicy': 'IfNotPresent',
        }
        self._container_registry_list = container_registry_list or []

        self._launch_param_list = self.parse_launch_param()

        # RosParameterList objects
        self._rosparam_list = self.parse_rosparam_from_manifest()

        # get container registry pull secret and policy
        self._parse_container_registry()

    def _parse_container_registry(self):
        """
        Get the container registry by name
        """
        req_container_registry = self._module_mani.get('containerRegistryName', None)

        # start from default values
        self._container_registry = {
            'imagePullSecret': '',
            'imagePullPolicy': 'IfNotPresent',
        }

        # if no registry list is available, keep defaults
        if not self._container_registry_list:
            return

        # use the default container registry
        if not req_container_registry:
            for item in self._container_registry_list:
                if item['name'] == 'default':
                    self._container_registry['imagePullSecret'] = item['imagePullSecretName']
                    self._container_registry['imagePullPolicy'] = item['imagePullPolicy']
                    break
            return

        # find the required container registry
        for item in self._container_registry_list:
            if item['name'] == req_container_registry:
                self._container_registry['imagePullSecret'] = item['imagePullSecretName']
                self._container_registry['imagePullPolicy'] = item['imagePullPolicy']
                return

    @property
    def container_registry(self):
        return self._container_registry

    def parse_rosparam_from_manifest(self) -> RosParameterList:
        """
        Return the required ros parameters as a list of RosParameter objects.
        This method is used in the RobotEntity to schedule the bind rosmodules.
        """
        return RosParameterList(self._module_mani.get('rosParameters', []))

    def get_rosparam_list(self) -> RosParameterList:
        return self._rosparam_list

    def get_launch_param(self):
        return self._launch_param_list

    def get_launch_param_device(self):
        param_dev = []
        for param in self._launch_param_list:
            if param['type'] == 'device':
                param_dev.append(param)
        return param_dev

    def get_launch_param_rosparam(self):
        param_rosparam = []
        for param in self._launch_param_list:
            if param['type'] == 'rosparam':
                param_rosparam.append(param)
        return param_rosparam

    def parse_launch_param(self):
        """
        Parse the launch parameters and sort them by the letter case.
        Upper case: device specific parameters
        Lower case: value from attached rosparameters
        """

        param_list = []
        launch_param_dict = self._module_mani.get('launchParameters', None)

        # if no launch parameter is required
        # return empty list
        if not launch_param_dict:
            return param_list

        for param, val in launch_param_dict.items():

            if not isinstance(val, dict) or len(val.keys()) == 0:
                continue

            param_val = list(val.keys())[0]

            # split the provided launch parameter into namespace and key
            # for rosparam:
            #      Given: {launch_parameters.use_sim}
            #           namespace: launch_parameters
            #           key: use_sim
            # for device:
            #      Given: {SIM_ARM.ROBOT_IP}
            #           namespace: sim_arm
            #           key: robot_ip
            if '.' not in param_val:
                continue

            namespace, key = param_val.split('.', 1)

            param_list.append({
                'param': param,
                'type': self.check_launch_param_type(param_val),
                'namespace': namespace,
                'key': key,
            })
        return param_list

    @staticmethod
    def check_launch_param_type(param_key: str) -> str:
        """
        Check the launch parameter type.
        """
        if param_key.isupper():
            return 'device'
        elif param_key.islower():
            return 'rosparam'
        else:
            return 'unknown'

    def calculate_node_score(self, node_state: dict):
        pass

    def get_bind_ros_module(self,
                            name: str,
                            selector_type: str,
                            target_node: str,
                            ) -> RosModule:
        pass

    @property
    def name(self):
        """
        Return the rosmodule name.
        """
        return self._module_mani['name']

    @property
    def scope(self) -> str:
        """
        Return the deployment scope of the ros module.
        Supported values: 'robot' (default) and 'fleet'.
        """
        return self._module_mani.get('scope', 'robot').lower()

    @property
    def is_fleet_scoped(self) -> bool:
        """
        Return True when this module should be deployed only once for the fleet.
        """
        return self.scope == 'fleet'

    @property
    def service(self) -> dict:
        """
        Optional Kubernetes Service definition for this ROS module.
        Example:
          service:
            enabled: true
            name: fleet-discovery-server
            type: ClusterIP
            ports:
              - name: dds-discovery
                port: 11811
                targetPort: 11811
                protocol: UDP
        """
        return self._module_mani.get('service', {}) or {}

    @property
    def service_enabled(self) -> bool:
        return bool(self.service.get('enabled', False))

    @property
    def service_name(self) -> str:
        return self.service.get('name', self.name)

    @property
    def service_type(self) -> str:
        return self.service.get('type', 'ClusterIP')

    @property
    def service_ports(self) -> list:
        ports = self.service.get('ports', [])
        if ports:
            return ports

        return [{
            'name': 'default',
            'port': 11811,
            'targetPort': 11811,
            'protocol': 'UDP',
        }]

    @property
    def workload_kind(self) -> str:
        """Return Pod for legacy modules or Deployment for managed modules."""
        workload_kind = self._module_mani.get('workloadKind', 'Pod')
        if workload_kind not in ['Pod', 'Deployment']:
            raise ValueError(
                f'Unsupported workloadKind <{workload_kind}> for module <{self.name}>.'
            )
        return workload_kind

    @property
    def replicas(self) -> int:
        """Return the desired replica count for a managed workload."""
        replicas = self._module_mani.get('replicas', 1)
        if not isinstance(replicas, int) or replicas < 1:
            raise ValueError(f'replicas for module <{self.name}> must be a positive integer.')
        return replicas

    @property
    def probes(self) -> dict:
        """Return Kubernetes container probes supplied AS-IS by the manifest."""
        return {
            key: self._module_mani[key]
            for key in ['startupProbe', 'readinessProbe', 'livenessProbe']
            if key in self._module_mani
        }

    @property
    def resources(self) -> dict:
        """Return Kubernetes container resource requests and limits AS-IS."""
        return self._module_mani.get('resources', {}) or {}

    @property
    def source_ros_setup(self) -> bool:
        """Return whether the container includes the selected ROS installation."""
        return bool(self._module_mani.get('sourceRosSetup', True))

    @property
    def preference(self) -> str:
        """
        Get the deployment preference of the rosmodule.
        If no preference is specified, return 'onboard' as default.
        Return:
            str: 'onboard' | 'edge' | 'cloud'
        """
        pref = self._module_mani.get('preference', [])

        if isinstance(pref, str):
            return pref

        if len(pref) == 0:
            return 'onboard'
        else:
            return pref[0]

    @property
    def peripheral_devices(self) -> List[str]:
        """
        Return the name list of the required peripheral devices.
        """
        return self._requirements.get('peripheral_devices', [])

    @property
    def requirements(self) -> dict:
        return self._requirements

    @property
    def privileged(self) -> bool:
        """
        Return privilege mode
        """
        return self._requirements.get('privileged', False)

    @property
    def container_image(self) -> dict:
        """
        Return the container image name and url
        """
        return {
            'image_name': self._module_mani['name'],
            'image_url': self._module_mani['image']
        }

    @property
    def required_launch_param_list(self) -> List[str]:

        required_launch_param = self._module_mani.get('requiredLaunchParamList', {})
        return required_launch_param.keys()

    @property
    def entrypoint(self):
        """
        Return the command that will be executed.
        """
        return self._module_mani['entrypoint']

    @property
    def source_ws(self):
        """
        Return the path to the setup.bash
        Format: /workspace/install/
            with slash at the end
        """
        source_ws = self._module_mani.get('sourceWs', WORKSPACE_PATH_DEFAULT)
        if not source_ws:
            return None
        if not source_ws.endswith('/'):
            source_ws += '/'
        return source_ws

    def __repr__(self) -> str:
        return f'<RosModule: {self.name}>'
