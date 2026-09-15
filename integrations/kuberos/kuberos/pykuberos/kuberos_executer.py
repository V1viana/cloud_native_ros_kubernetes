# Python
from copy import deepcopy
from datetime import datetime, timezone
import sys
import time
import logging
import os
from typing import List

# Kubernetes
import kubernetes
from kubernetes import client
from kubernetes.stream import stream
from kubernetes.client.rest import ApiException
from urllib3.exceptions import MaxRetryError


logger = logging.getLogger('pykuberos')
logger.propagate = False


DELETE_POD_GRACE_TIME_PERIOD = 3 # seconds


class KubeConfig():
    """
    Kubenetes cluster config object
    """

    def __init__(self,
                k8s_config_dict: dict,
                 ) -> None:

        self._cluster_config = kubernetes.client.Configuration()

        service_token = k8s_config_dict['service_token']
        host_url = k8s_config_dict['host_url']
        ca_cert_path = k8s_config_dict['ca_cert_path']
        if service_token == '__IN_CLUSTER__':
            service_account = '/var/run/secrets/kubernetes.io/serviceaccount'
            with open(f'{service_account}/token', encoding='utf-8') as stream:
                service_token = stream.read().strip()
            ca_cert_path = f'{service_account}/ca.crt'
            host = os.environ.get('KUBERNETES_SERVICE_HOST', 'kubernetes.default.svc')
            port = os.environ.get('KUBERNETES_SERVICE_PORT_HTTPS', '443')
            host_url = f'https://{host}:{port}'

        self._cluster_config.host = host_url
        self._cluster_config.api_key['authorization'] = service_token
        self._cluster_config.api_key_prefix['authorization'] = 'Bearer'
        self._cluster_config.ssl_ca_cert = ca_cert_path

        # self.kube_config_file = "path_to_config"
        # self.context = "context in this config"
        # self.token = "token"

    @property
    def cluster_config(self) -> dict:
        """
        Return the cluster config object
        """
        return self._cluster_config


class ExecutionResponse():
    """
    Execution response class for KubeROS tasks.
    """

    def __init__(self) -> None:
        self._status = 'pending'
        self._data = {}
        self._errors = []
        self._msgs = []

    def clear(self) -> None:
        self.__init__()

    def set_success(self) -> None:
        self._status = 'success'

    def set_data(self, data: dict) -> None:
        """
        set the data to the response.
        """
        self._data = data

    def add_msg(self, msg: str) -> None:
        """
        Add the message to the response.
        """
        self._msgs.append(msg)

    def _add_error(self,
                   err_reason: str,
                   err_msg: str,
                   err_msg_verbose: str = '') -> None:
        if not isinstance(err_reason, str):
            raise ValueError("The err_reason must be a string.")
        if not isinstance(err_msg, str):
            raise ValueError("The err_msg must be a string.")

        self._errors.append({
            'reason': err_reason,
            'err_msg': err_msg,
            'msg_verbose': err_msg_verbose
        })

    def raise_api_exception_error(self,
                                  exc: ApiException) -> None:
        """
        Add the error message from ApiException to the response.
        """

        self._add_error(
            err_reason=exc.reason,
            err_msg=self.parse_error_reason(exc),
            err_msg_verbose=exc.body
        )
        self._status = 'failed'

    def parse_error_reason(self,
                        exc: ApiException) -> str:
        """
        Return a more readable error message for KubeROS users
        """
        reason = exc.reason
        if reason == 'Unauthorized':
            return 'Cluster service account token is invalid or expired.'

        if reason == 'Conflict':
            return 'Kubernetes resource already exists.'

        if reason == 'Not Found':
            return 'Kubernetes resource not found.'

        return 'Cluster is not reachable.'


    def set_rejected(self,
                     reason: str,
                     msg: str = None) -> None:
        self._status = 'rejected'
        self._add_error(
            err_reason=reason,
            err_msg=msg
        )

    def set_failed(self,
                   reason: str,
                   err_msg: str) -> None:
        """
        Set the response status as failed.
        """
        self._status = 'failed'
        self._add_error(reason, err_msg)

    def to_dict(self) -> dict:
        """
        Get the response as a dict.
        """
        return {
            'status': self._status,
            'data': self._data,
            'errors': self._errors,
            'msgs': self._msgs
        }


class KubernetesExecuter():
    """
    Handler to interact with Kubernetes cluster.
    Provides following functionalities:
        - create/delete namespaces
        - create/delete pods
        - create/delete services
        - create/delete configmaps
        - create/delete daemonsets
    """

    def __init__(self,
                 kube_config: dict,
                 namespace: str = None,
                 ) -> None:
        """
        Args:
            - kube_config: {
                'name': 'kubernetes',
                'host_url': 'https://xxxxx:6443',
                'service_token': 'admin-token-xxxxx',
                'ca_cert_path': '/home/xxxxx/ca.crt',
            }
        """
        self._kube_config = KubeConfig(kube_config)
        self._ns = (
            namespace
            or os.environ.get('KUBEROS_TARGET_NAMESPACE')
            or 'ros-default'
        )

        self._kube_client = kubernetes.client.ApiClient(
            self._kube_config.cluster_config
        )
        
        self._kube_core_api = kubernetes.client.CoreV1Api(self._kube_client)
        self._kube_apps_api = kubernetes.client.AppsV1Api(self._kube_client)

        self._response = ExecutionResponse()

        # logger.debug("KubernetesExecuter initialized.")


    def __exit__(self, exc_type, exc_value, traceback):
        self._kube_client.close()


    ### NAMESPACE ###
    def create_namespace(self,
                         namespace: str) -> ExecutionResponse:
        """
        Create a new namespace
        """
        try:
            existed_ns = self._kube_core_api.list_namespace(_request_timeout=1)
        except ApiException as exc:
            self._response.raise_api_exception_error(exc)
            return self._response.to_dict()
        except MaxRetryError:
            self._response.set_failed(
                reason='UnexpectedError',
                err_msg=f'Unexpected error: {sys.exc_info()[0]}'
            )
            print(self._response.to_dict())
            sys.exit(1)

        # check if the namespace already exists
        for item in existed_ns.items:
            if item.metadata.name == namespace:
                # logger.debug("Namespace <%s> already exists.", namepace)
                self._response.set_success()
                self._response.set_data(
                    self._kube_client.sanitize_for_serialization(item)
                )
                self._response.add_msg(
                    f'Namespace <{namespace}> already exists.')
                return self._response.to_dict()

        # namespace manifest
        ns_manifest = {
            'apiVersion': 'v1',
            'kind': 'Namespace',
            'metadata': {
                'name': namespace,
            }
        }

        try:
            res = self._kube_core_api.create_namespace(body=ns_manifest)
            self._response.set_data(res)
            self._response.set_success()

        except ApiException as exc:
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    ### NODE ###
    def get_nodes_status(self,
                   node_selector = None,
                   get_namespaced_pods = False) -> ExecutionResponse:
        """
        Get node status in the cluster
        TODO: TimeoutError
        """
        try:
            res = self._kube_core_api.list_node()

            if get_namespaced_pods:
                ns_pods = self._kube_core_api.list_namespaced_pod(
                    namespace=self._ns
                )
            
            nodes = []
            for item in res.items:
                node = {
                    'name': item.metadata.name,
                    'labels': item.metadata.labels,
                    'status': self._kube_client.sanitize_for_serialization(item.status),
                    'ready': self.check_node_readiness(item), # bool
                }
                
                # Get the pods in the namespace
                # For BatchJob scheduler
                # TODO Review
                if get_namespaced_pods:
                    node['status']['pods'] = []
                    for pod in ns_pods.items:
                        
                        if pod.spec.node_name == node['name']:
                            node['status']['pods'].append(
                                {'namespace': self._ns,
                                 'pod': self._kube_client.sanitize_for_serialization(pod)}
                            )

                nodes.append(node)

            self._response.set_data(nodes)
            self._response.set_success()
        except ApiException as exc:
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    @staticmethod
    def check_node_readiness(node: dict) -> bool:
        """
        Find the node readiness status from the node status conditions.
        """
        try:
            conditions = node.status.conditions
        except AttributeError:
            return False

        if conditions is None:
            return False

        for condition in conditions:
            if condition.type == 'Ready':
                if condition.status == 'Unknown':
                    return False
                return condition.status

        # If no 'Ready' condition is found, return 'Unknown'
        return False


    def label_node(self,
                   node_name: str,
                   labels: dict) -> ExecutionResponse:
        """
        Add labels to the nodes.
        Label is crucial as identifier for the node selection in the deployment
        It must be careful maintained, to avoid any inconsistences and conficts.

        args:
            - node_name: str
            - labels: dict, e.g. {
                    'resource.kuberos.io/type': 'onboard',
                    'robot.kuberos.io/name': 'dummy-1'
                }
        """
        try:
            node = self._kube_core_api.read_node(name=node_name)
            node.metadata.labels.update(labels)
            res = self._kube_core_api.patch_node(name=node_name,
                                                 body=node)
            
            new_labels = res.metadata.labels
            self._response.set_data(new_labels)
            self._response.set_success()

        except ApiException as exc:
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    ### POD ###
    def create_pod(self,
                   pod_manifest: dict) -> ExecutionResponse:
        """
        Create a pod in a given namespace

        Args:
            - pod_manifest: pod manifest in dict format
        """

        try:
            res = self._kube_core_api.create_namespaced_pod(
                body=pod_manifest,
                namespace=self._ns,
            )
            self._response.set_data(res)
            self._response.set_success()

        except ApiException as exc:
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    def check_pod_status(self,
                         pod_name: str) -> ExecutionResponse:
        """
        Check the pod status
        """
        # simplified pod status
        pod_status = {
            'name': pod_name,
            'status': '',
            'container_status': '',
            'pod_ip': '',
            'reason': '',
            'msg': None
            }

        try:
            res = self._kube_core_api.read_namespaced_pod_status(
                namespace=self._ns,
                name=pod_name
            )
            pod_status['status'] = res.status.phase
            pod_status['container_status'] = self._kube_client.sanitize_for_serialization(
                res.status.container_statuses)
            pod_status['pod_ip'] = res.status.pod_ip
            pod_status['msg'] = res.status.message
            pod_status['reason'] = res.status.reason
            pod_status['conditions'] = self._kube_client.sanitize_for_serialization(
                res.status.conditions)

            # check if the pod is in the terminating state
            if res.metadata.deletion_timestamp is not None:
                
                pod_status['status'] = 'Terminating'

            self._response.set_data(pod_status)
            self._response.set_success()

        except ApiException as exc:
            if exc.reason == 'Not Found':
                pod_status['status'] = 'NotFound'
                self._response.set_data(pod_status)
                self._response.set_success()
            else:
                self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    def delete_pod(self,
                   pod_name: str) -> ExecutionResponse:
        """
        Delete a pod
        """
        try:
            res = self._kube_core_api.delete_namespaced_pod(
                namespace=self._ns,
                name=pod_name,
                grace_period_seconds=DELETE_POD_GRACE_TIME_PERIOD
            )
            self._response.set_data(res)
            self._response.set_success()

        except ApiException as exc:
            if exc.reason == 'Not Found':
                self._response.set_success()
            else:
                self._response.raise_api_exception_error(exc)

        return self._response.to_dict()

    ### DEPLOYMENT ###
    def create_deployment(self,
                          deployment_manifest: dict) -> ExecutionResponse:
        """Create an apps/v1 Deployment in the configured namespace."""
        try:
            res = self._kube_apps_api.create_namespaced_deployment(
                body=deployment_manifest,
                namespace=self._ns,
            )
            self._response.set_data(res)
            self._response.set_success()
        except ApiException as exc:
            self._response.raise_api_exception_error(exc)
        return self._response.to_dict()

    def check_deployment_status(self,
                                deployment_name: str) -> ExecutionResponse:
        """Return a Pod-compatible phase derived from Deployment readiness."""
        deployment_status = {
            'name': deployment_name,
            'resource_kind': 'Deployment',
            'status': '',
            'ready_replicas': 0,
            'replicas': 0,
            'reason': '',
            'msg': None,
        }
        try:
            res = self._kube_apps_api.read_namespaced_deployment_status(
                namespace=self._ns,
                name=deployment_name,
            )
            desired = res.spec.replicas or 0
            ready = res.status.ready_replicas or 0
            deployment_status['ready_replicas'] = ready
            deployment_status['replicas'] = desired
            deployment_status['conditions'] = self._kube_client.sanitize_for_serialization(
                res.status.conditions
            )
            deployment_status['status'] = 'Running' if ready >= desired else 'Pending'
            for condition in res.status.conditions or []:
                if (
                    condition.type == 'Progressing'
                    and condition.status == 'False'
                    and condition.reason == 'ProgressDeadlineExceeded'
                ):
                    deployment_status['status'] = 'Failed'
                    deployment_status['reason'] = condition.reason
                    deployment_status['msg'] = condition.message
                    break
            if res.metadata.deletion_timestamp is not None:
                deployment_status['status'] = 'Terminating'
            self._response.set_data(deployment_status)
            self._response.set_success()
        except ApiException as exc:
            if self._is_not_found(exc):
                deployment_status['status'] = 'NotFound'
                self._response.set_data(deployment_status)
                self._response.set_success()
            else:
                self._response.raise_api_exception_error(exc)
        return self._response.to_dict()

    def delete_deployment(self,
                          deployment_name: str) -> ExecutionResponse:
        """Delete an apps/v1 Deployment and its owned Pods."""
        try:
            res = self._kube_apps_api.delete_namespaced_deployment(
                namespace=self._ns,
                name=deployment_name,
                propagation_policy='Foreground',
            )
            self._response.set_data(res)
            self._response.set_success()
        except ApiException as exc:
            if self._is_not_found(exc):
                self._response.set_success()
            else:
                self._response.raise_api_exception_error(exc)
        return self._response.to_dict()

    def list_pod_in_namespace(self,
                              namespace: str = None) -> dict:
        """
        List all pods in the given namespace
        """
        if not namespace:
            namespace = self._ns
            
        try: 
            res = self._kube_core_api.list_namespaced_pod(
                namespace=namespace
            )
            self._response.set_data(res)
            self._response.set_success()
        except ApiException as exc:
            self._response.raise_api_exception_error(exc)
        
        return self._response.to_dict()


    ### SERVICE ###
    def create_service(self,
                       svc_manifest: dict) -> ExecutionResponse:
        """
        Create a service in a given namespace

        Args:
            - svc_manifest: service manifest in dict format
        """

        try:
            res = self._kube_core_api.create_namespaced_service(
                body=svc_manifest,
                namespace=self._ns
            )
            self._response.set_data(res)
            self._response.set_success()

        except ApiException as exc:
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()

    def create_kubernetes_resource(self,
                                   manifest: dict) -> ExecutionResponse:
        """
        Create a generic Kubernetes resource generated by the scheduler.
        This is needed because rosModules can now generate both Pods and
        Services, not only Pods.
        """
        kind = manifest.get('kind', 'Pod')

        if kind == 'Pod':
            return self.create_pod(pod_manifest=manifest)

        if kind == 'Service':
            return self.create_service(svc_manifest=manifest)

        if kind == 'Deployment':
            return self.create_deployment(deployment_manifest=manifest)

        self._response.set_failed(
            reason='UnsupportedKubernetesResource',
            err_msg=f'Unsupported Kubernetes resource kind: {kind}'
        )
        return self._response.to_dict()

    def check_service_status(self,
                             svc_name: str) -> ExecutionResponse:
        """
        Check the service status
        """
        svc_status = {
            'name': svc_name,
            'status': '',
            'cluster_ip': '',
            'ports': '',
            'reason': '',
            'msg': None
        }

        try:
            res = self._kube_core_api.read_namespaced_service_status(
                namespace=self._ns,
                name=svc_name
            )
            svc_status['status'] = 'Found'
            svc_status['cluster_ip'] = res.spec.cluster_ip
            svc_status['ports'] = self._kube_client.sanitize_for_serialization(res.spec.ports)
            self._response.set_data(svc_status)
            self._response.set_success()

        except ApiException as exc:
            if exc.reason == 'Not Found':
                svc_status['status'] = 'NotFound'
                self._response.set_data(svc_status)
                self._response.set_success()
            else:
                self._response.raise_api_exception_error(exc)

        return self._response.to_dict()

    def ensure_pod(self, pod_manifest: dict) -> dict:
        """
        Reuse pod if it already exists, otherwise create it.
        Used for robot-scoped resources like discovery server.
        """
        name = pod_manifest['metadata']['name']

        check_res = self.check_pod_status(pod_name=name)
        if check_res['status'] == 'success':
            pod_status = check_res.get('data', {})
            phase = pod_status.get('status', '')

            # Reuse if the pod already exists and is not terminating
            if phase not in ['NotFound', '', 'Terminating']:
                resp = ExecutionResponse()
                resp.set_success()
                resp.set_data(pod_status)
                resp.add_msg(f'Pod <{name}> already exists, reused.')
                return resp.to_dict()

        return self.create_kubernetes_resource(manifest=pod_manifest)


    def ensure_service(self, svc_manifest: dict) -> dict:
        """
        Reuse service if it already exists, otherwise create it.
        Used for robot-scoped resources like discovery server.
        """
        name = svc_manifest['metadata']['name']

        check_res = self.check_service_status(svc_name=name)
        if check_res['status'] == 'success':
            svc_status = check_res.get('data', {})

            if svc_status.get('status') == 'Found':
                resp = ExecutionResponse()
                resp.set_success()
                resp.set_data(svc_status)
                resp.add_msg(f'Service <{name}> already exists, reused.')
                return resp.to_dict()

        return self.create_service(svc_manifest=svc_manifest)

    def delete_service(self,
                       svc_name: str) -> ExecutionResponse:
        """
        Delete a service
        """
        try:
            res = self._kube_core_api.delete_namespaced_service(
                namespace=self._ns,
                name=svc_name
            )
            self._response.set_data(res)
            self._response.set_success()

        except ApiException as exc:
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()



    ### CONFIGMAP ###
    def create_configmap(self,
                         name: str,
                         content: dict) -> ExecutionResponse:
        """
        Create a configmap in a given namespace

        Args:
            - name: str - name of the configmap
            - content: dict - content of the configmap
        """
        
        configmap=client.V1ConfigMap(
            api_version='v1',
            kind='ConfigMap',
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self._ns
            ),
            data=content, 
        )

        try:
            res=self._kube_core_api.create_namespaced_config_map(
                namespace=self._ns,
                body=configmap
            )
            
            self._response.set_data(
                self._kube_client.sanitize_for_serialization(res)
            )
            self._response.set_success()

        except ApiException as exc:
            
            logger.error("[KubeCient] - [FailedToCreateConfigMap] - %s", exc)
            
            logger.debug("[KubeCient] - Received content: %s", content)
            
            self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    def delete_configmap(self,
                         name: str) -> ExecutionResponse:
        """
        Delete a configmap in a given namespace

        Args:
            - name: str - name of the configmap
        """
        logger.debug("[Kube Client] Deleting Configmap: %s", name)

        try:
            res=self._kube_core_api.delete_namespaced_config_map(
                namespace=self._ns,
                name=name
            )

            # check the response status
            if res.status == 'Success': # snippet from response: {..., "status": "Success"}
                self._response.set_data(self._kube_client.sanitize_for_serialization(res))
                self._response.set_success()
            else:
                self._response.set_failed(
                    reason='FailedToDeleteConfigmap',
                    err_msg='Failed to delete configmap.'
                )
        
        # if the configmap is not found, set the response as success
        except ApiException as exc:
            if exc.reason == 'Not Found':
                
                self._response.set_success()
            else:
                self._response.raise_api_exception_error(exc)

        return self._response.to_dict()


    def create_or_update_container_access_token(self,
                                        secret_name: str,
                                        docker_config_json: dict,
                                        update: bool = False):
        """
        Create or update the container access token.
        
        Args:
            - secret_name: name of the secret
            - encoded_secret: base64 encoded string of the docker config file
        """
        secret = client.V1Secret(
            api_version='v1',
            kind='Secret',
            metadata=client.V1ObjectMeta(name=secret_name),
            type='kubernetes.io/dockerconfigjson',
            data=docker_config_json
        )
        msg = ''
        if update:
            self._kube_core_api.delete_namespaced_secret(
                name=secret_name,
                namespace=self._ns,
                body=client.V1DeleteOptions(
                    propagation_policy="Foreground",
                    # grace_period_seconds=5  # wait 5 seconds before deleting
                )
            )
        
        try:
            res = self._kube_core_api.create_namespaced_secret(
                namespace=self._ns,
                body=secret
                )
            print(res)
            msg = "Created container access token {}".format(secret_name)
            print(msg)
            return True, msg 
        except ApiException as e:
            print(e)
            msg = "Error creating container access token {}".format(secret_name)
            print (msg)
            return False, msg
    
    def get_resource_usage(self):
        """
        Get nodes resource usage
        via kubernetes state metrics server
        TODO: Review and cleaning
        TODO: Rename to get_node_metrics
        """

        # Connect to the CustomObjects API to fetch metrics
        custom_api = client.CustomObjectsApi(self._kube_client)

        # Define the API endpoint details
        group = 'metrics.k8s.io'
        version = 'v1beta1'
        plural = 'nodes'
        
        try:
            # Fetch metrics for all nodes
            metrics = custom_api.list_cluster_custom_object(group, version, plural)
            
            # # Iterate over nodes and print CPU usage
            # for item in metrics['items']:
            #     node_name = item['metadata']['name']
            #     cpu_usage_nano_cores = int(item['usage']['cpu'].rstrip('n'))
            #     # cpu_usage_millicores = cpu_usage_nano_cores / 10**6
            #     # print(f"Node: {node_name} - CPU Usage: {cpu_usage_millicores}")

            self._response.set_data(metrics['items'])
            self._response.set_success()
        except ApiException as e:
            print(e)
            self._response.raise_api_exception_error(e)
            
        return self._response.to_dict()

    def get_pod_metrics(self):
        """
        Get pods resource usage
        via kubernetes state metrics server
        """
        custom_api = client.CustomObjectsApi(self._kube_client)

        # Define the API endpoint details
        group = 'metrics.k8s.io'
        version = 'v1beta1'
        plural = 'pods'
        
        try:
            metrics = custom_api.list_namespaced_custom_object(group, version, self._ns, plural)
            
            for pod in metrics['items']:
                print(pod)
            
            self._response.set_data(metrics['items'])
            self._response.set_success()
            
        except ApiException as e:
            print(e)
            self._response.raise_api_exception_error(e)
            
        return self._response.to_dict()   
        
    
    def write_file_to_pod(self, pod_name, data):
        """
        cat << EOF > file.test
        cd "$HOME"
        echo "$PWD" # echo the current path
        EOF\n
        """

        try:
            exec_cmd = ['/bin/sh']
            resp = stream(self._kube_core_api.connect_get_namespaced_pod_exec,
                        pod_name,
                        self._ns,
                        command=exec_cmd,
                        stderr=True, stdin=True,
                        stdout=True, tty=False,
                        _preload_content=False)

            # add the content to the command
            cmd = []
            for item in data:
                content = item['content']
                dst_path = item['dst_path']
                
                cmd.append(f'cat << EOF > {dst_path}')
                for line in content:
                    cmd.append(line)
                cmd.append('EOF\n')
                
             # write to the file
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    logger.info("STDOUT: %s" % resp.read_stdout())
                if resp.peek_stderr():
                    logger.error("STDERR: %s" % resp.read_stderr())
                if cmd:
                    c = cmd.pop(0)
                    print("Running command... %s\n" % c)
                    resp.write_stdin(c + "\n")
                else:
                    break
                
            resp.close()
            self._response.set_success()
        # resp.write_stdin("date\n")
        # sdate = resp.readline_stdout(timeout=3)
        # print("Server date command returns: %s" % sdate)
        # resp.write_stdin("whoami\n")
        # user = resp.readline_stdout(timeout=3)
        # print("Server user is: %s" % user)
        
        except ApiException as e:
            logger.error("Writing file to pod - Exception occurred")
            self._response.raise_api_exception_error(e)
    
        return self._response.to_dict()   


class KuberosExecuter(KubernetesExecuter):
    """
        Interface to interact with Kubernetes cluster,
        contains multiple operations to realize the whole functionality.
        It includes:
         - prepare new namespace for deployment
         - create dds servers and services
         -
    """
    def __init__(self,
                 kube_config: dict,  # Union[dict, KubeConfig] = KubeConfig(),
                 namespace: str=None,
                 ) -> None:
        super().__init__(kube_config=kube_config,
                         namespace=namespace)


    def deploy_disc_server(self, 
                        disc_server_list: list) -> dict:
        """
        Deploy discovery server(s) for ONE robot.
        Primary server and a secondary (backup) server as optional.

        Robot-scoped behavior:
        - if discovery pod/service already exist, reuse them
        - otherwise create only the missing resources
        """

        response = ExecutionResponse()

        # create namespace if needed
        ns_res = self.create_namespace(namespace=self._ns)
        if ns_res['status'] == 'failed':
            return ns_res

        deployed = []

        for disc_server in disc_server_list:
            pod = disc_server['pod']
            svc = disc_server['svc']

            # service first, then pod
            svc_res = self.ensure_service(svc_manifest=svc)
            if svc_res['status'] == 'failed':
                return svc_res

            pod_res = self.ensure_pod(pod_manifest=pod)
            if pod_res['status'] == 'failed':
                return pod_res

            deployed.append({
                'dds_pod': pod['metadata']['name'],
                'dds_service': svc['metadata']['name'],
                'pod_result': pod_res,
                'service_result': svc_res,
            })

        response.set_data({
            'discovery_resources': deployed,
            'namespace': self._ns,
        })
        response.set_success()

        return response.to_dict()


    def deploy_rosmodules(self,
                           pod_list: list) -> dict:
        """
        Deploy a list of ROS modules in the cluster.
        
        Args:
            - pod_list: list of pod manifest dict.
        """

        resource_list = []

        for pod in pod_list:
            try: 
                res = self.create_kubernetes_resource(manifest=pod)
                
                if res['status'] == 'failed':
                    return res
                
                resource_list.append({
                    'kind': pod.get('kind', 'Pod'),
                    'name': pod['metadata']['name'],
                })

            except Exception as exc:
                # catch unknown exception 
                logger.fatal("Failed to create resource: %s", pod['metadata']['name'])
                logger.fatal(exc)
                self._response.set_failed(
                    reason='FailedToCreateResource',
                    err_msg=str(exc),
                )
                return self._response.to_dict()
        
        self._response.set_data({
            'ros_resources': resource_list,
            'ros_pods': [
                item['name'] for item in resource_list if item['kind'] == 'Pod'
            ],
            'ros_deployments': [
                item['name'] for item in resource_list
                if item['kind'] == 'Deployment'
            ],
            'namespace': self._ns
             })
        self._response.set_success()
        
        return self._response.to_dict()


    def check_deployed_pod_status(self,
                                  pod_list: list) -> dict:
        """
        Get the status of the deployed pods.
        """
        try: 
            for pod in pod_list:
                resource_kind = pod.get('resource_kind', 'Pod')
                if resource_kind == 'Deployment':
                    check_res = self.check_deployment_status(
                        deployment_name=pod['name']
                    )
                else:
                    check_res = self.check_pod_status(pod_name=pod['name'])
                pod.update(check_res['data'])
            self._response.set_data(pod_list)
            self._response.set_success()
            return self._response.to_dict()
        
        except Exception as exc:
            # catch unknown exception
            logger.fatal("Failed to check pod status: %s", pod['name'])
            self._response.set_failed(
                reason='FailedToCheckPodStatus',
                err_msg=str(exc),
            )
            return self._response.to_dict()   


    def check_deployed_svc_status(self,
                                  svc_list: list) -> dict:
        """
        Get the status of the deployed services.
        """
        try:
            for svc in svc_list:
                # logger.debug("Check svc status: %s", svc['name'])
                check_res=self.check_service_status(svc_name=svc['name'])
                svc.update(check_res['data'])
            self._response.set_data(svc_list)
            self._response.set_success()
            return self._response.to_dict()
        
        except Exception as exc:
            # catch unknown exception
            logger.fatal("Failed to check svc status: %s", svc['name'])
            self._response.set_failed(
                reason='FailedToCheckSvcStatus',
                err_msg=str(exc),
            )
            return self._response.to_dict()

        
    def delete_rosmodules(self,
                          pod_list: list,
                          svc_list: list=[]) -> None:
        """
        Delete rosmodules in the cluster.
        """
        deleted_resources = []
        try:
            for pod in pod_list:
                if isinstance(pod, dict):
                    resource_name = pod['name']
                    resource_kind = pod.get('resource_kind', 'Pod')
                else:
                    resource_name = pod
                    resource_kind = 'Pod'
                if resource_kind == 'Deployment':
                    self.delete_deployment(deployment_name=resource_name)
                else:
                    self.delete_pod(pod_name=resource_name)
                deleted_resources.append({
                    'kind': resource_kind,
                    'name': resource_name,
                })
            for svc in svc_list:
                self.delete_service(svc_name=svc)
                deleted_resources.append({
                    'kind': 'Service',
                    'name': svc,
                })
            self._response.set_data({
                'deleted_resources': deleted_resources,
            })
            self._response.set_success()
        except Exception as exc:
            self._response.set_data({
                'deleted_resources': deleted_resources,
            })
            self._response.set_failed(
                reason='FailedToDeletePod',
                err_msg=str(exc),
            )
        return self._response.to_dict()


    @staticmethod
    def _resource_map(resources: list) -> dict:
        resource_map = {}
        for manifest in resources:
            kind = manifest.get('kind', 'Pod')
            name = manifest.get('metadata', {}).get('name')
            if kind not in ['Pod', 'Service', 'Deployment']:
                raise ValueError(f'Unsupported Kubernetes resource kind: {kind}')
            if not name:
                raise ValueError('Kubernetes resource metadata.name is required.')
            key = (kind, name)
            if key in resource_map:
                raise ValueError(f'Duplicate Kubernetes resource: {kind}/{name}')
            resource_map[key] = manifest
        return resource_map

    @staticmethod
    def _is_not_found(exc: ApiException) -> bool:
        return getattr(exc, 'status', None) == 404 or exc.reason == 'Not Found'

    def _wait_for_pod_absent(self, pod_name: str, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self._kube_core_api.read_namespaced_pod_status(
                    namespace=self._ns,
                    name=pod_name,
                )
                time.sleep(0.5)
            except ApiException as exc:
                if self._is_not_found(exc):
                    return
                raise
        raise TimeoutError(f'Timed out waiting for Pod <{pod_name}> deletion.')

    @staticmethod
    def _pod_configmap_names(pod_manifest: dict) -> set:
        names = set()
        if pod_manifest.get('kind') == 'Deployment':
            pod_spec = (
                pod_manifest.get('spec', {})
                .get('template', {})
                .get('spec', {})
            )
        else:
            pod_spec = pod_manifest.get('spec', {})

        for volume in pod_spec.get('volumes', []):
            configmap = volume.get('configMap', {})
            if configmap.get('name'):
                names.add(configmap['name'])

            for source in volume.get('projected', {}).get('sources', []):
                projected_configmap = source.get('configMap', {})
                if projected_configmap.get('name'):
                    names.add(projected_configmap['name'])

        containers = (
            pod_spec.get('initContainers', [])
            + pod_spec.get('containers', [])
        )
        for container in containers:
            for env_var in container.get('env', []):
                configmap_ref = env_var.get('valueFrom', {}).get('configMapKeyRef', {})
                if configmap_ref.get('name'):
                    names.add(configmap_ref['name'])

            for env_source in container.get('envFrom', []):
                configmap_ref = env_source.get('configMapRef', {})
                if configmap_ref.get('name'):
                    names.add(configmap_ref['name'])

        return names

    def _wait_for_pod_running(self, pod_name: str, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                pod = self._kube_core_api.read_namespaced_pod_status(
                    namespace=self._ns,
                    name=pod_name,
                )
            except ApiException as exc:
                if self._is_not_found(exc):
                    time.sleep(0.5)
                    continue
                raise

            phase = getattr(pod.status, 'phase', None)
            if phase in ['Running', 'Succeeded']:
                return
            if phase == 'Failed':
                raise RuntimeError(f'Pod <{pod_name}> entered Failed phase.')
            time.sleep(0.5)

        raise TimeoutError(f'Timed out waiting for Pod <{pod_name}> to become Running.')

    def _wait_for_deployment_absent(
        self, deployment_name: str, timeout_seconds: int
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self._kube_apps_api.read_namespaced_deployment_status(
                    namespace=self._ns,
                    name=deployment_name,
                )
                time.sleep(0.5)
            except ApiException as exc:
                if self._is_not_found(exc):
                    return
                raise
        raise TimeoutError(
            f'Timed out waiting for Deployment <{deployment_name}> deletion.'
        )

    def _wait_for_deployment_running(
        self, deployment_name: str, timeout_seconds: int
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                deployment = self._kube_apps_api.read_namespaced_deployment_status(
                    namespace=self._ns,
                    name=deployment_name,
                )
            except ApiException as exc:
                if self._is_not_found(exc):
                    time.sleep(0.5)
                    continue
                raise

            desired = deployment.spec.replicas or 0
            replicas = deployment.status.replicas or 0
            ready = deployment.status.ready_replicas or 0
            available = deployment.status.available_replicas or 0
            unavailable = deployment.status.unavailable_replicas or 0
            updated = deployment.status.updated_replicas or 0
            observed = deployment.status.observed_generation or 0
            generation = deployment.metadata.generation or 0
            # A rolling update can have an old ready Pod while the updated Pod
            # is failing. Require complete convergence of the ReplicaSets.
            if (
                replicas == desired
                and ready == desired
                and available == desired
                and unavailable == 0
                and updated == desired
                and observed >= generation
            ):
                return
            for condition in deployment.status.conditions or []:
                if (
                    condition.type == 'Progressing'
                    and condition.status == 'False'
                    and condition.reason == 'ProgressDeadlineExceeded'
                ):
                    raise RuntimeError(
                        f'Deployment <{deployment_name}> exceeded its progress deadline.'
                    )
            time.sleep(0.5)
        raise TimeoutError(
            f'Timed out waiting for Deployment <{deployment_name}> rollout.'
        )

    def upsert_configmaps(self, configmap_list: List[dict]) -> dict:
        response = ExecutionResponse()
        updated = []

        try:
            for configmap in configmap_list:
                name = configmap['name']
                body = client.V1ConfigMap(
                    api_version='v1',
                    kind='ConfigMap',
                    metadata=client.V1ObjectMeta(name=name, namespace=self._ns),
                    data=configmap['content'],
                )
                try:
                    self._kube_core_api.read_namespaced_config_map(
                        namespace=self._ns,
                        name=name,
                    )
                    self._kube_core_api.replace_namespaced_config_map(
                        namespace=self._ns,
                        name=name,
                        body=body,
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise
                    self._kube_core_api.create_namespaced_config_map(
                        namespace=self._ns,
                        body=body,
                    )
                updated.append(name)
        except Exception as exc:
            response.set_failed(
                reason='FailedToUpsertConfigMap',
                err_msg=str(exc),
            )
            return response.to_dict()

        response.set_data({'configmaps': updated, 'namespace': self._ns})
        response.set_success()
        return response.to_dict()

    def delete_configmaps_by_name(self, configmap_names: list) -> dict:
        response = ExecutionResponse()
        deleted = []

        try:
            for name in configmap_names:
                try:
                    self._kube_core_api.delete_namespaced_config_map(
                        namespace=self._ns,
                        name=name,
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise
                deleted.append(name)
        except Exception as exc:
            response.set_failed(
                reason='FailedToDeleteConfigMap',
                err_msg=str(exc),
            )
            return response.to_dict()

        response.set_data({'configmaps': deleted, 'namespace': self._ns})
        response.set_success()
        return response.to_dict()

    def reconcile_rosmodules(
        self,
        old_resources: list,
        new_resources: list,
        timeout_seconds: int = 90,
        changed_configmaps: list = None,
    ) -> dict:
        """
        Differentially reconcile Pod/Deployment ROS modules and their Services.

        Discovery resources are intentionally not passed to this method.
        Unchanged workloads remain running. Deployments use a rolling update;
        legacy Pods are replaced and can experience module-local downtime.
        """
        response = ExecutionResponse()

        try:
            changed_configmaps = set(changed_configmaps or [])
            old_map = self._resource_map(old_resources)
            new_map = self._resource_map(new_resources)

            old_services = {
                key: manifest for key, manifest in old_map.items()
                if key[0] == 'Service'
            }
            new_services = {
                key: manifest for key, manifest in new_map.items()
                if key[0] == 'Service'
            }
            old_pods = {
                key: manifest for key, manifest in old_map.items()
                if key[0] == 'Pod'
            }
            new_pods = {
                key: manifest for key, manifest in new_map.items()
                if key[0] == 'Pod'
            }
            old_deployments = {
                key: manifest for key, manifest in old_map.items()
                if key[0] == 'Deployment'
            }
            new_deployments = {
                key: manifest for key, manifest in new_map.items()
                if key[0] == 'Deployment'
            }

            common_service_keys = old_services.keys() & new_services.keys()
            changed_service_keys = {
                key for key in common_service_keys
                if old_services[key] != new_services[key]
            }
            added_service_keys = new_services.keys() - old_services.keys()
            removed_service_keys = old_services.keys() - new_services.keys()
            preserved_service_keys = common_service_keys - changed_service_keys

            for key in sorted(changed_service_keys | added_service_keys):
                _, name = key
                manifest = new_services[key]
                try:
                    self._kube_core_api.read_namespaced_service(
                        namespace=self._ns,
                        name=name,
                    )
                    self._kube_core_api.patch_namespaced_service(
                        namespace=self._ns,
                        name=name,
                        body=manifest,
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise
                    self._kube_core_api.create_namespaced_service(
                        namespace=self._ns,
                        body=manifest,
                    )

            for _, name in sorted(removed_service_keys):
                try:
                    self._kube_core_api.delete_namespaced_service(
                        namespace=self._ns,
                        name=name,
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise

            common_deployment_keys = (
                old_deployments.keys() & new_deployments.keys()
            )
            changed_deployment_keys = {
                key for key in common_deployment_keys
                if old_deployments[key] != new_deployments[key]
            }
            configmap_deployment_keys = set()
            if changed_configmaps:
                configmap_deployment_keys = {
                    key for key in common_deployment_keys
                    if changed_configmaps & (
                        self._pod_configmap_names(old_deployments[key])
                        | self._pod_configmap_names(new_deployments[key])
                    )
                }
                changed_deployment_keys.update(configmap_deployment_keys)

            added_deployment_keys = (
                new_deployments.keys() - old_deployments.keys()
            )
            removed_deployment_keys = (
                old_deployments.keys() - new_deployments.keys()
            )
            preserved_deployment_keys = (
                common_deployment_keys - changed_deployment_keys
            )

            for _, name in sorted(removed_deployment_keys):
                try:
                    self._kube_apps_api.delete_namespaced_deployment(
                        namespace=self._ns,
                        name=name,
                        propagation_policy='Foreground',
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise

            for _, name in sorted(removed_deployment_keys):
                self._wait_for_deployment_absent(name, timeout_seconds)

            deployment_keys_to_apply = (
                changed_deployment_keys | added_deployment_keys
            )
            for key in sorted(deployment_keys_to_apply):
                _, name = key
                manifest = deepcopy(new_deployments[key])
                if key in configmap_deployment_keys:
                    annotations = (
                        manifest.setdefault('spec', {})
                        .setdefault('template', {})
                        .setdefault('metadata', {})
                        .setdefault('annotations', {})
                    )
                    annotations['kuberos.io/config-restarted-at'] = (
                        datetime.now(timezone.utc).isoformat()
                    )
                try:
                    self._kube_apps_api.read_namespaced_deployment(
                        namespace=self._ns,
                        name=name,
                    )
                    self._kube_apps_api.patch_namespaced_deployment(
                        namespace=self._ns,
                        name=name,
                        body=manifest,
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise
                    self._kube_apps_api.create_namespaced_deployment(
                        namespace=self._ns,
                        body=manifest,
                    )

            for _, name in sorted(deployment_keys_to_apply):
                self._wait_for_deployment_running(name, timeout_seconds)

            common_pod_keys = old_pods.keys() & new_pods.keys()
            changed_pod_keys = {
                key for key in common_pod_keys
                if old_pods[key] != new_pods[key]
            }
            if changed_configmaps:
                changed_pod_keys.update({
                    key for key in common_pod_keys
                    if changed_configmaps & (
                        self._pod_configmap_names(old_pods[key])
                        | self._pod_configmap_names(new_pods[key])
                    )
                })

            added_pod_keys = new_pods.keys() - old_pods.keys()
            removed_pod_keys = old_pods.keys() - new_pods.keys()
            preserved_pod_keys = common_pod_keys - changed_pod_keys
            pod_keys_to_delete = changed_pod_keys | removed_pod_keys
            pod_keys_to_create = changed_pod_keys | added_pod_keys

            for _, name in sorted(pod_keys_to_delete):
                try:
                    self._kube_core_api.delete_namespaced_pod(
                        namespace=self._ns,
                        name=name,
                        grace_period_seconds=DELETE_POD_GRACE_TIME_PERIOD,
                    )
                except ApiException as exc:
                    if not self._is_not_found(exc):
                        raise

            for _, name in sorted(pod_keys_to_delete):
                self._wait_for_pod_absent(name, timeout_seconds)

            for key in sorted(pod_keys_to_create):
                self._kube_core_api.create_namespaced_pod(
                    namespace=self._ns,
                    body=new_pods[key],
                )

            for _, name in sorted(pod_keys_to_create):
                self._wait_for_pod_running(name, timeout_seconds)

        except Exception as exc:
            response.set_failed(
                reason='FailedToReconcileRosModules',
                err_msg=str(exc),
            )
            return response.to_dict()

        response.set_data({
            'pods': sorted(name for _, name in new_pods),
            'deployments': sorted(name for _, name in new_deployments),
            'services': sorted(name for _, name in new_services),
            'created_pods': sorted(name for _, name in added_pod_keys),
            'replaced_pods': sorted(name for _, name in changed_pod_keys),
            'deleted_pods': sorted(name for _, name in removed_pod_keys),
            'preserved_pods': sorted(name for _, name in preserved_pod_keys),
            'created_deployments': sorted(
                name for _, name in added_deployment_keys
            ),
            'updated_deployments': sorted(
                name for _, name in changed_deployment_keys
            ),
            'deleted_deployments': sorted(
                name for _, name in removed_deployment_keys
            ),
            'preserved_deployments': sorted(
                name for _, name in preserved_deployment_keys
            ),
            'created_services': sorted(name for _, name in added_service_keys),
            'updated_services': sorted(name for _, name in changed_service_keys),
            'deleted_services': sorted(name for _, name in removed_service_keys),
            'preserved_services': sorted(name for _, name in preserved_service_keys),
            'namespace': self._ns,
        })
        response.set_success()
        return response.to_dict()


    def deploy_configmaps(self,
                        configmap_list: List[dict]) -> dict:
        """
        Create ConfigMaps from list 

        Args: 
            - configmap_list: list of configmap dict
        """
        response = ExecutionResponse()

        # Important: empty list is a valid case
        if not configmap_list:
            response.set_success()
            response.set_data([])
            response.add_msg('No configmaps to deploy.')
            return response.to_dict()

        deployed_configmaps = []

        for configmap in configmap_list:
            try:
                res = self.create_configmap(
                    name=configmap['name'],
                    content=configmap['content'],
                )

                if res['status'] == 'failed':
                    return res

                deployed_configmaps.append(configmap['name'])

            except Exception as exc:
                logger.fatal("Failed to create configmap: %s", configmap['name'])
                logger.fatal(exc)

                response.set_failed(
                    reason='FailedToCreateConfigmap',
                    err_msg=str(exc)
                )
                return response.to_dict()

        response.set_data({
            'configmaps': deployed_configmaps,
            'namespace': self._ns
        })
        response.set_success()
        return response.to_dict()


    def delete_deployed_configmaps(self,
                          configmap_list) -> dict:
        """
        Delete all ConfigMaps in the list
        """
        for configmap in configmap_list:
            try:
                res = self.delete_configmap(name=configmap['name'])
                if res['status'] == 'failed':
                    # Retrurn the failure response and break the loop
                    return res
                self._response.set_data(res)
            except Exception as exc: 
                logger.fatal("Failed to delete configmap: %s", configmap['name'])
                logger.fatal(exc)
                self._response.set_failed(
                    reason='FailedToDeleteConfigmap',
                    err_msg=str(exc)
                )
                self._response.to_dict()
        
        # if no exception, set the response as success
        self._response.set_success()
        return self._response.to_dict()
    
