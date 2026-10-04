# Modified by the cloud_native_ros_kubernetes project (2026) from KubeROS (kuberos-io/kuberos commit 0253c9e).
# See THIRD_PARTY_NOTICES (repository root; in the container images: /usr/share/licenses/cloud-native-ros/THIRD_PARTY_NOTICES).
"""
Job Scheduler generates the pod list and svc for batch jobs
"""

# python
import logging
import copy

# Pykuberos
from .manifest import DeploymentManifest
from .rosmodule import RosModule, DiscoveryServer
from .rosparameter import RosParamMapList


logger = logging.getLogger('scheduler')
logger.propagate = False

RESERVED_CPU_REQUEST = 0.3


class JobScheduler():
    """
    Select the edge node for the job
    Maximize the cluster utilization -> cpu over 60, incl. startup process, average value
    """

    def __init__(self,
                 next_job_list: list,
                 deployment_manifest: dict,
                 cluster_state: dict) -> None:
        """
        Initialize the KuberosScheduler
        param: deployment_manifest: dict - deployment manifest

        param: next_job_list: list - the next job list
            example: {
                'job_uuid': self.get_uuid(),
                'group_postfix': self.batch_job_group.group_postfix,
                'job_postfix': self.slug,
                'volume': self.volume,
                'manifest': self.deployment_manifest,
            }

        param: cluster_state: dict - the cluster state for batch jobs
            {
                'cluster_name': 'kube',
                'nodes': [{
                    'hostname': 'kube-edge-worker-02',
                    'is_allocatable': True,
                    'cpu': 3.968162422,
                    'cpu_allow': 3,
                    'memory': 6.118108,
                    'memory_allow': 6,
                    'storage': 3.2684154680000006,
                    'num_pods': 0},
            ]}
        """

        self._deployment_manifest = DeploymentManifest(deployment_manifest)
        self._rmw_impl = self._normalize_rmw_implementation(
            getattr(self._deployment_manifest, 'rmw_implementation', 'fastdds')
        )

        self._job_spec = self._parse_batch_job_spec(deployment_manifest)

        self._next_job_list = next_job_list

        self._rosparam_maps = RosParamMapList(self._deployment_manifest.rosparam_map)

        self._cluster_state = cluster_state

        self.scheduled_jobs = []

        self._disc_server = None

        self._configmaps = []

        self._volumes = []

        self._rosmodules = []

        self._sc_result = []

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
        return self._rmw_impl == 'fastdds'

    def schedule(self) -> dict:

        # loop through the cluster node list
        for node in self._cluster_state['nodes']:

            logger.debug("NODE STATE: %s", node)

            # check if the node is allocatable
            allocatable = self.check_node_allocability(node, self._job_spec)

            # if this node is not allocatable, skip it
            if not allocatable:
                logger.warning("[Job Scheduler] Node %s is not allocatable", node['hostname'])
                continue

            # if there is no more job to schedule, break the loop
            if len(self._next_job_list) == 0:
                logger.info("[Job Scheduler] No more jobs to schedule")
                break

            job = self._next_job_list.pop()

            # schedule the job to the node
            logger.debug("[Job Scheduler] Scheduling job %s to node %s", job['job_uuid'], node['hostname'])
            sc_job = self.schedule_one(node, job)
            self._sc_result.append(sc_job)

        return self._sc_result

    def schedule_one(self, node, job) -> dict:
        """
        Schedule one batch job
        """
        sc_job = {
            'disc_server': {},
            'configmaps': [],
            'volumes': [],
            'rosmodules': [],
        }

        # requested resources
        resources = {}
        job_requested_resources = self._job_spec.get('resources', None)
        if job_requested_resources is not None:
            requests = job_requested_resources.get('requests', None)
            if requests is not None:
                resources = {'requests': {}}

                if requests.get('cpu', None) is not None:
                    resources['requests']['cpu'] = requests.get('cpu', 0)

                if requests.get('memory', None) is not None:
                    resources['requests']['memory'] = requests.get('memory', 0)

                if resources['requests'] == {}:
                    resources = {}

            # use the optimal resource request, if it is specified
            optimals = job_requested_resources.get('optimals', None)
            if optimals is not None:
                if optimals.get('cpu', 0) < node['cpu_allow']:
                    # node has sufficient cpu resources, use the optimal setup to improve the performance
                    logger.debug(
                        "Node <%s> has sufficient CPU resources [Allocable: %s], use the optimals: %s",
                        node['hostname'],
                        node['cpu_allow'],
                        optimals['cpu']
                    )
                    resources.setdefault('requests', {})
                    resources['requests']['cpu'] = optimals['cpu']

                if optimals.get('memory', 0) < node['memory_allow']:
                    resources.setdefault('requests', {})
                    resources['requests']['memory'] = optimals['memory']

        hostname = node['hostname']
        pod_name_postfix = f"{job['group_postfix']}-{job['job_postfix']}"

        sc_job['job_uuid'] = job['job_uuid']
        sc_job['group_postfix'] = job['group_postfix']

        sc_job['disc_server'] = self.schedule_discovery_server(
            hostname=hostname,
            pod_name_postfix=pod_name_postfix,
            requested_resources=resources,
            volume=job['volume']
        )

        sc_job['configmaps'] = self.schedule_configmaps(
            configmap_postfix=job['group_postfix']
        )
        sc_job['volumes'] = self.schedule_volumes()

        sc_job['rosmodules'] = self.schedule_rosmodules(
            hostname=hostname,
            group_postfix=job['group_postfix'],
            job_postfix=job['job_postfix'],
            volume=job['volume']
        )
        sc_job['cluster_node_info'] = node

        return sc_job

    def _parse_batch_job_spec(self, deployment_manifest) -> dict:
        """
        Get the batch job spec from the deployment manifest.
        If the job spec is not fully specified, use the default values.
        """
        job_spec = {
            'maxRetry': 1,
            'startupTimeout': 300,
            'runningTimeout': 300,
            'resources': {
                'numProNode': 0,
                'requests': {
                    'cpu': 1
                }
            }
        }

        job_spec_from_manifest = deployment_manifest.get('jobSpec', None)

        if not job_spec_from_manifest:
            return job_spec

        job_spec_from_manifest = copy.deepcopy(job_spec_from_manifest)
        job_spec_from_manifest.setdefault('resources', {})
        job_spec_from_manifest['resources'].setdefault('requests', {})
        job_spec_from_manifest['resources'].setdefault('optimals', {})

        try:
            # requested cpu
            cpu = job_spec_from_manifest['resources']['requests'].get('cpu', 0)
            if isinstance(cpu, str):
                cpu = float(cpu.replace('m', '')) / 1000
                job_spec_from_manifest['resources']['requests']['cpu'] = cpu

            # optimal cpu
            cpu_opt = job_spec_from_manifest['resources']['optimals'].get('cpu', 0)
            if isinstance(cpu_opt, str):
                cpu_opt = float(cpu_opt.replace('m', '')) / 1000

            if cpu_opt > cpu:
                job_spec_from_manifest['resources']['optimals']['cpu'] = cpu_opt

        except Exception:
            job_spec_from_manifest['resources']['requests']['cpu'] = 0

        job_spec.update(job_spec_from_manifest)

        return job_spec

    @staticmethod
    def check_node_allocability(node: dict,
                                job_spec) -> bool:
        """
        Check whether the node can be used for the job
        # TODO: Filter with labels: The pod of number is not equal the number of jobs!
        """

        # number of pods
        num_pods_requested = job_spec['resources']['numProNode']
        if num_pods_requested > 0:
            num_pods_on_node = node['num_pods']
            if num_pods_requested <= num_pods_on_node:
                logger.warning(
                    "Node <%s> has %s pods running, %s pods requested",
                    node['hostname'], num_pods_on_node, num_pods_requested
                )
                return False

        # CPU usage
        cpu_requested = job_spec['resources']['requests']['cpu']
        cpu_available = node['cpu']
        cpu_allocable = node['cpu_allow']
        if cpu_requested > cpu_available:
            logger.warning(
                "Node <%s> has %s CPU available, %s CPU requested",
                node['hostname'], cpu_available, cpu_requested
            )
            return False
        if cpu_requested > cpu_allocable - RESERVED_CPU_REQUEST:
            logger.warning(
                "Node <%s> has %s CPU allocable, %s CPU requested. [RESERVED: %s]",
                node['hostname'],
                cpu_allocable - RESERVED_CPU_REQUEST,
                cpu_requested,
                RESERVED_CPU_REQUEST
            )
            return False

        return True

    def schedule_discovery_server(self,
                                  hostname,
                                  pod_name_postfix,
                                  requested_resources,
                                  volume) -> dict:
        """
        Schedule a discovery server
        """
        if not self._uses_fastdds_discovery():
            self._disc_server = None
            return {}

        self._disc_server = DiscoveryServer(
            name=f'batch-job-disc-server-{pod_name_postfix}',
            port=11811,
            target_node=hostname,
            add_env_for_introspection=False,
            requested_resources=requested_resources,
            volumes=[volume] if volume else [],
            # skip_running=True,
        )

        return {
            'pod': self._disc_server.pod_manifest,
            'svc': self._disc_server.service_manifest,
        }

    def schedule_configmaps(self, configmap_postfix: str = ''):
        """
        Return configmaps for the deployment.
        For batch jobs, prefix the configmap names so each group can have its own resources.
        """
        configmaps = self._rosparam_maps.get_all_configmaps_for_deployment()

        if not configmap_postfix:
            self._configmaps = configmaps
            return self._configmaps

        prefixed_configmaps = []
        for configmap in configmaps:
            configmap_copy = copy.deepcopy(configmap)
            configmap_copy['name'] = f"{configmap_postfix}-{configmap_copy['name']}"
            prefixed_configmaps.append(configmap_copy)

        self._configmaps = prefixed_configmaps
        return self._configmaps

    def schedule_volumes(self) -> list:
        return []

    def schedule_rosmodules(self,
                            hostname: str,
                            group_postfix: str,
                            job_postfix: str,
                            volume: dict,
                            ) -> list:
        rosmodules = []

        discovery_svc_name = (
            self._disc_server.discovery_svc_name
            if self._disc_server is not None else None
        )

        for module_mani in self._deployment_manifest.rosmodules_mani:
            pod_name = f'{group_postfix}-{module_mani.name}-{job_postfix}'
            sc_module = RosModule(
                name=pod_name,
                discovery_svc_name=discovery_svc_name,
                node_selector_type='node',
                target=hostname,
                volumes=[volume] if volume else [],
                container_image=module_mani.container_image,
                image_pull_secret=module_mani.container_registry['imagePullSecret'],
                image_pull_policy=module_mani.container_registry['imagePullPolicy'],
                entrypoint=module_mani.entrypoint,
                source_ws=module_mani.source_ws,
                privileged=module_mani.privileged,
                rmw_implementation=self._rmw_impl,
            )

            # Attach ros parameters and environment variables
            self._attach_required_rosparameter_and_env_var(
                scheduled_module=sc_module,
                module_manifest=module_mani,
                rosparam_maps=self._rosparam_maps,
                configmap_postfix=group_postfix
            )
            rosmodules.append(sc_module.get_kubernetes_manifest())

        return rosmodules

    def _attach_required_rosparameter_and_env_var(self,
                                                  scheduled_module: RosModule,
                                                  module_manifest,
                                                  rosparam_maps: RosParamMapList,
                                                  configmap_postfix: str = '') -> None:
        """
        Attach required ROS parameters to the scheduled module.

        :param scheduled_module: The ROS module scheduled to be attached.
        :param module_manifest: ROS module manifest.
        :param rosparam_maps: List containing ROS parameter maps.
        :param configmap_postfix: Postfix to be added to the configmap name to distinguish different groups.
        """

        # Get the required rosparameters from ROSModuleManifest
        req_rosparam_list = module_manifest.get_rosparam_list()
        # Match the corresponding RosParamMap
        # Find the custom rosparam from the RosParamMap
        req_rosparam_list.match_rosparam_map_list(rosparam_maps)

        # Get the required ros2 launch args
        req_launch_param = module_manifest.get_launch_param()
        launch_rosparam_list = module_manifest.get_launch_param_rosparam()

        # logger.debug("Required launch param: %s", req_launch_param)

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
            else:
                prefixed_configmap_name = (
                    f"{configmap_postfix}-{configmap.get('name')}"
                    if configmap_postfix else configmap.get('name')
                )

                if req_rosparam.type == 'yaml':
                    # attach the configmap to scheduled ros module
                    # mount the configmap to the container
                    scheduled_module.attach_configmap_yaml(
                        configmap_name=prefixed_configmap_name,
                        mount_path=req_rosparam.mount_path
                    )

                if req_rosparam.type == 'key-value':
                    # add ENV variables with valueFrom - configMapKeyRef
                    # append the launch parameters with arg_value from the configmap
                    confgmap_copy = copy.deepcopy(configmap)
                    confgmap_copy['name'] = prefixed_configmap_name
                    scheduled_module.attach_configmap_key_value(
                        configmap=confgmap_copy,
                        launch_param_list=launch_rosparam_list,
                    )