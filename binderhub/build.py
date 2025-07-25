"""
Contains build of a docker image from a git repository.
"""

import datetime
import json
import os
import threading
import warnings
from collections import defaultdict
from enum import Enum
from typing import Union
from urllib.parse import urlparse

import kubernetes.config
from kubernetes import client, watch
from tornado.ioloop import IOLoop
from tornado.log import app_log
from traitlets import Any, Bool, Dict, Integer, List, Unicode, default
from traitlets.config import LoggingConfigurable

from .utils import KUBE_REQUEST_TIMEOUT, ByteSpecification, rendezvous_rank


class ProgressEvent:
    """
    Represents an event that happened in the build process
    """

    class Kind(Enum):
        """
        The kind of event that happened
        """

        BUILD_STATUS_CHANGE = 1
        LOG_MESSAGE = 2

    class BuildStatus(Enum):
        """
        The state the build is now in

        Used when `kind` is `Kind.BUILD_STATUS_CHANGE`
        """

        PENDING = "pending"
        RUNNING = "running"
        BUILT = "built"
        FAILED = "failed"
        UNKNOWN = "unknown"

    def __init__(self, kind: Kind, payload: Union[str, BuildStatus]):
        self.kind = kind
        self.payload = payload


class BuildExecutor(LoggingConfigurable):
    """Base class for a build of a version controlled repository to a self-contained
    environment
    """

    q = Any(
        help="Queue that receives progress events after the build has been submitted",
    )

    name = Unicode(
        help=(
            "A unique name for the thing (repo, ref) being built."
            "Used to coalesce builds, make sure they are not being unnecessarily repeated."
        ),
    )

    repo_url = Unicode(help="URL of repository to build.")

    ref = Unicode(help="Ref of repository to build.")

    image_name = Unicode(help="Full name of the image to build. Includes the tag.")

    git_credentials = Unicode(
        "",
        help=(
            "Git credentials to use when cloning the repository, passed via the GIT_CREDENTIAL_ENV environment variable."
            "Can be anything that will be accepted by git as a valid output from a git-credential helper. "
            "See https://git-scm.com/docs/gitcredentials for more information."
        ),
        config=True,
    )

    push_secret = Unicode(
        "",
        help="Implementation dependent static secret for pushing image to a registry.",
        config=True,
    )

    registry_credentials = Dict(
        {},
        help=(
            "Implementation dependent credentials for pushing image to a registry. "
            "For example, if push tokens are temporary this could be used to pass "
            "dynamically created credentials "
            '`{"registry": "docker.io", "username":"user", "password":"password"}`. '
            "This will be JSON encoded and passed in the environment variable "
            "CONTAINER_ENGINE_REGISTRY_CREDENTIALS` to repo2docker. "
            "If provided this will be used instead of push_secret."
        ),
        config=True,
    )

    memory_limit = ByteSpecification(
        0,
        help="Memory limit for the build process in bytes (optional suffixes K M G T).",
        config=True,
    )

    appendix = Unicode(
        "",
        help="Appendix to be added at the end of the Dockerfile used by repo2docker.",
        config=True,
    )

    builder_info = Dict(
        help=(
            "Metadata about the builder e.g. repo2docker version. "
            "This is included in the BinderHub version endpoint"
        ),
        config=True,
    )

    repo2docker_extra_args = List(
        Unicode,
        default_value=[],
        help="""
        Extra commandline parameters to be passed to jupyter-repo2docker during build
        """,
        config=True,
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.main_loop = IOLoop.current()

    stop_event = Any()

    @default("stop_event")
    def _default_stop_event(self):
        return threading.Event()

    def get_r2d_cmd_options(self):
        """Get options/flags for repo2docker"""
        r2d_options = [
            f"--ref={self.ref}",
            f"--image={self.image_name}",
            "--no-clean",
            "--no-run",
            "--json-logs",
            "--user-name=jovyan",
            "--user-id=1000",
        ]
        if self.appendix:
            r2d_options.extend(["--appendix", self.appendix])

        if self.push_secret:
            r2d_options.append("--push")

        if self.memory_limit:
            r2d_options.append("--build-memory-limit")
            r2d_options.append(str(self.memory_limit))

        r2d_options += self.repo2docker_extra_args

        return r2d_options

    def get_cmd(self):
        """Get the cmd to run to build the image"""
        cmd = [
            "jupyter-repo2docker",
        ] + self.get_r2d_cmd_options()

        # repo_url comes at the end, since otherwise our arguments
        # might be mistook for commands to run.
        # see https://github.com/jupyter/repo2docker/pull/128
        cmd.append(self.repo_url)

        return cmd

    def progress(self, kind: ProgressEvent.Kind, payload: str):
        """
        Put current progress info into the queue on the main thread
        """
        self.main_loop.add_callback(self.q.put, ProgressEvent(kind, payload))

    def submit(self):
        """
        Run a build to create the image for the repository.

        Progress of the build can be monitored by listening for items in
        the Queue passed to the constructor as `q`.
        """
        raise NotImplementedError()

    def stream_logs(self):
        """
        Stream build logs to the queue in self.q
        """
        pass

    def cleanup(self):
        """
        Stream build logs to the queue in self.q
        """
        pass

    def stop(self):
        """
        Stop watching progress of build

        Frees up build watchers that are no longer hooked up to any current requests.
        This is not related to stopping the build.
        """
        self.stop_event.set()


class KubernetesBuildExecutor(BuildExecutor):
    """Represents a build of a git repository into a docker image.

    This ultimately maps to a single pod on a kubernetes cluster. Many
    different build objects can point to this single pod and perform
    operations on the pod. The code in this class needs to be careful and take
    this into account.

    For example, operations a Build object tries might not succeed because
    another Build object pointing to the same pod might have done something
    else. This should be handled gracefully, and the build object should
    reflect the state of the pod as quickly as possible.

    ``name``
        The ``name`` should be unique and immutable since it is used to
        sync to the pod. The ``name`` should be unique for a
        ``(repo_url, ref)`` tuple, and the same tuple should correspond
        to the same ``name``. This allows use of the locking provided by k8s
        API instead of having to invent our own locking code.

    """

    api = Any(
        help="Kubernetes API object to make requests (kubernetes.client.CoreV1Api())",
    )

    @default("api")
    def _default_api(self):
        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.ConfigException:
            kubernetes.config.load_kube_config()
        return client.CoreV1Api()

    # Overrides the default for BuildExecutor
    push_secret = Unicode(
        "binder-build-docker-config",
        help=(
            "Name of a Kubernetes secret containing static credentials for pushing "
            "an image to a registry."
        ),
        config=True,
    )

    registry_credentials = Dict(
        {},
        help=(
            "Implementation dependent credentials for pushing image to a registry. "
            "For example, if push tokens are temporary this could be used to pass "
            "dynamically created credentials "
            '`{"registry": "docker.io", "username":"user", "password":"password"}`. '
            "This will be JSON encoded and passed in the environment variable "
            "CONTAINER_ENGINE_REGISTRY_CREDENTIALS` to repo2docker. "
            "If provided this will be used instead of push_secret. "
            "Currently this is passed to the build pod as a plain text environment "
            "variable, though future implementations may use a Kubernetes secret."
        ),
        config=True,
    )

    namespace = Unicode(
        help="Kubernetes namespace to spawn build pods into", config=True
    )

    @default("namespace")
    def _default_namespace(self):
        return os.getenv("BUILD_NAMESPACE", "default")

    build_image = Unicode(
        "quay.io/jupyterhub/repo2docker:2024.07.0",
        help="Docker image containing repo2docker that is used to spawn the build pods.",
        config=True,
    )

    @default("builder_info")
    def _default_builder_info(self):
        return {"build_image": self.build_image}

    image_pull_secrets = List(
        [], help="Pull secrets for the builder image", config=True
    )

    docker_host = Unicode(
        "/var/run/docker.sock",
        allow_none=True,
        help=(
            "The docker socket to use for building the image. "
            "Must be a unix domain socket on a filesystem path accessible on the node "
            "in which the build pod is running. "
            "This is mounted into the build pod, set to None to disable, "
            "e.g. if you are using an alternative builder that doesn't need the docker socket."
        ),
        config=True,
    )

    cpu_request = Unicode(
        "",
        help=(
            "CPU request for the build pod (e.g. '100m', '0.5', '1'). "
            "This reserves CPU resources for the build pod in the kubernetes cluster. "
            "Can be specified as millicores (e.g. '100m') or as decimal cores (e.g. '0.5')."
        ),
        config=True,
    )

    memory_request = ByteSpecification(
        0,
        help=(
            "Memory request of the build pod in bytes (optional suffixes K M G T). "
            "The actual building happens in the docker daemon, "
            "but setting request in the build pod makes sure that memory is reserved for the docker build "
            "in the node by the kubernetes scheduler."
        ),
        config=True,
    )

    node_selector = Dict(
        {}, help="Node selector for the kubernetes build pod.", config=True
    )

    extra_envs = Dict(
        {},
        help="Extra environment variables for the kubernetes build pod.",
        config=True,
    )

    log_tail_lines = Integer(
        100,
        help=(
            "Number of log lines to fetch from a currently running build. "
            "If a build with the same name is already running when submitted, "
            "only the last `log_tail_lines` number of lines will be fetched and displayed to the end user. "
            "If not, all log lines will be streamed."
        ),
        config=True,
    )

    sticky_builds = Bool(
        False,
        help=(
            "If true, builds for the same repo (but different refs) will try to schedule on the same node, "
            "to reuse cache layers in the docker daemon being used."
        ),
        config=True,
    )

    _component_label = Unicode("binderhub-build")

    def get_affinity(self):
        """Determine the affinity term for the build pod.

        There are a two affinity strategies, which one is used depends on how
        the BinderHub is configured.

        In the default setup the affinity of each build pod is an "anti-affinity"
        which causes the pods to prefer to schedule on separate nodes.

        In a setup with docker-in-docker enabled pods for a particular
        repository prefer to schedule on the same node in order to reuse the
        docker layer cache of previous builds.
        """
        resp = self.api.list_namespaced_pod(
            self.namespace,
            label_selector="component=image-builder,app=binder",
            _request_timeout=KUBE_REQUEST_TIMEOUT,
            _preload_content=False,
        )
        image_builder_pods = json.loads(resp.read())

        if self.sticky_builds and image_builder_pods:
            node_names = [
                pod["spec"]["nodeName"] for pod in image_builder_pods["items"]
            ]
            ranked_nodes = rendezvous_rank(node_names, self.repo_url)
            best_node_name = ranked_nodes[0]

            affinity = client.V1Affinity(
                node_affinity=client.V1NodeAffinity(
                    preferred_during_scheduling_ignored_during_execution=[
                        client.V1PreferredSchedulingTerm(
                            weight=100,
                            preference=client.V1NodeSelectorTerm(
                                match_expressions=[
                                    client.V1NodeSelectorRequirement(
                                        key="kubernetes.io/hostname",
                                        operator="In",
                                        values=[best_node_name],
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        else:
            affinity = client.V1Affinity(
                pod_anti_affinity=client.V1PodAntiAffinity(
                    preferred_during_scheduling_ignored_during_execution=[
                        client.V1WeightedPodAffinityTerm(
                            weight=100,
                            pod_affinity_term=client.V1PodAffinityTerm(
                                topology_key="kubernetes.io/hostname",
                                label_selector=client.V1LabelSelector(
                                    match_labels=dict(component=self._component_label)
                                ),
                            ),
                        )
                    ]
                )
            )

        return affinity

    def get_builder_volumes(self):
        """
        Get the lists of volumes and volume-mounts for the build pod.
        """
        volume_mounts = []
        volumes = []

        if self.docker_host is not None:
            volume_mounts.append(
                client.V1VolumeMount(
                    mount_path="/var/run/docker.sock", name="docker-socket"
                )
            )
            docker_socket_path = urlparse(self.docker_host).path
            volumes.append(
                client.V1Volume(
                    name="docker-socket",
                    host_path=client.V1HostPathVolumeSource(
                        path=docker_socket_path, type="Socket"
                    ),
                )
            )

        if not self.registry_credentials and self.push_secret:
            volume_mounts.append(
                client.V1VolumeMount(
                    mount_path="/root/.docker/config.json",
                    name="docker-config",
                    sub_path="config.json",
                )
            )
            volumes.append(
                client.V1Volume(
                    name="docker-config",
                    secret=client.V1SecretVolumeSource(secret_name=self.push_secret),
                )
            )

        return volumes, volume_mounts

    def get_image_pull_secrets(self):
        """
        Get the list of image pull secrets to be used for the builder image
        """

        image_pull_secrets = []

        for secret in self.image_pull_secrets:
            image_pull_secrets.append(client.V1LocalObjectReference(name=secret))

        return image_pull_secrets

    def submit(self):
        """
        Submit a build pod to create the image for the repository.

        Progress of the build can be monitored by listening for items in
        the Queue passed to the constructor as `q`.
        """
        volumes, volume_mounts = self.get_builder_volumes()

        env = [
            client.V1EnvVar(name=key, value=value)
            for key, value in self.extra_envs.items()
        ]
        if self.git_credentials:
            env.append(
                client.V1EnvVar(name="GIT_CREDENTIAL_ENV", value=self.git_credentials)
            )

        if self.registry_credentials:
            env.append(
                client.V1EnvVar(
                    name="CONTAINER_ENGINE_REGISTRY_CREDENTIALS",
                    value=json.dumps(self.registry_credentials),
                )
            )

        self.pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=self.name,
                labels={
                    "name": self.name,
                    "component": self._component_label,
                },
                annotations={
                    "binder-repo": self.repo_url,
                },
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        image=self.build_image,
                        name="builder",
                        args=self.get_cmd(),
                        volume_mounts=volume_mounts,
                        resources=client.V1ResourceRequirements(
                            limits={"memory": self.memory_limit},
                            requests={
                                "memory": self.memory_request,
                                **(
                                    {}
                                    if not self.cpu_request
                                    else {"cpu": self.cpu_request}
                                ),
                            },
                        ),
                        env=env,
                    )
                ],
                tolerations=[
                    client.V1Toleration(
                        key="hub.jupyter.org/dedicated",
                        operator="Equal",
                        value="user",
                        effect="NoSchedule",
                    ),
                    # GKE currently does not permit creating taints on a node pool
                    # with a `/` in the key field
                    client.V1Toleration(
                        key="hub.jupyter.org_dedicated",
                        operator="Equal",
                        value="user",
                        effect="NoSchedule",
                    ),
                ],
                node_selector=self.node_selector,
                volumes=volumes,
                restart_policy="Never",
                affinity=self.get_affinity(),
                image_pull_secrets=self.get_image_pull_secrets(),
            ),
        )

        try:
            _ = self.api.create_namespaced_pod(
                self.namespace,
                self.pod,
                _request_timeout=KUBE_REQUEST_TIMEOUT,
            )
        except client.rest.ApiException as e:
            if e.status == 409:
                # Someone else created it!
                app_log.info("Build %s already running", self.name)
                pass
            else:
                raise
        else:
            app_log.info("Started build %s", self.name)

        app_log.info("Watching build pod %s", self.name)
        while not self.stop_event.is_set():
            w = watch.Watch()
            try:
                for f in w.stream(
                    self.api.list_namespaced_pod,
                    self.namespace,
                    label_selector=f"name={self.name}",
                    timeout_seconds=30,
                    _request_timeout=KUBE_REQUEST_TIMEOUT,
                ):
                    if f["type"] == "DELETED":
                        phase = f["object"].status.phase
                        app_log.debug(
                            "Pod %s was deleted with phase %s",
                            f["object"].metadata.name,
                            phase,
                        )
                        if phase == "Succeeded":
                            self.progress(
                                ProgressEvent.Kind.BUILD_STATUS_CHANGE,
                                ProgressEvent.BuildStatus.BUILT,
                            )
                        else:
                            self.progress(
                                ProgressEvent.Kind.BUILD_STATUS_CHANGE,
                                ProgressEvent.BuildStatus.FAILED,
                            )
                        return
                    self.pod = f["object"]
                    if not self.stop_event.is_set():
                        # Account for all the phases kubernetes pods can be in
                        # Pending, Running, Succeeded, Failed, Unknown
                        # https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-phase
                        phase = self.pod.status.phase
                        if phase == "Pending":
                            self.progress(
                                ProgressEvent.Kind.BUILD_STATUS_CHANGE,
                                ProgressEvent.BuildStatus.PENDING,
                            )
                        elif phase == "Running":
                            self.progress(
                                ProgressEvent.Kind.BUILD_STATUS_CHANGE,
                                ProgressEvent.BuildStatus.RUNNING,
                            )
                        elif phase == "Succeeded":
                            # Do nothing! We will clean this up, and send a 'Completed' progress event
                            # when the pod has been deleted
                            pass
                        elif phase == "Failed":
                            self.progress(
                                ProgressEvent.Kind.BUILD_STATUS_CHANGE,
                                ProgressEvent.BuildStatus.FAILED,
                            )
                        elif phase == "Unknown":
                            self.progress(
                                ProgressEvent.Kind.BUILD_STATUS_CHANGE,
                                ProgressEvent.BuildStatus.UNKNOWN,
                            )
                        else:
                            # This shouldn't happen, unless k8s introduces new Phase types
                            warnings.warn(
                                f"Found unknown phase {phase} when building {self.name}"
                            )

                    if self.pod.status.phase == "Succeeded":
                        self.cleanup()
                    elif self.pod.status.phase == "Failed":
                        self.cleanup()
            except Exception:
                app_log.exception("Error in watch stream for %s", self.name)
                raise
            finally:
                w.stop()
            if self.stop_event.is_set():
                app_log.info("Stopping watch of %s", self.name)
                return

    def stream_logs(self):
        """
        Stream build logs to the queue in self.q
        """
        app_log.info("Watching logs of %s", self.name)
        for line in self.api.read_namespaced_pod_log(
            self.name,
            self.namespace,
            follow=True,
            tail_lines=self.log_tail_lines,
            _request_timeout=(3, None),
            _preload_content=False,
        ):
            if self.stop_event.is_set():
                app_log.info("Stopping logs of %s", self.name)
                return
            # verify that the line is JSON
            line = line.decode("utf-8")
            try:
                json.loads(line)
            except ValueError:
                # log event wasn't JSON.
                # use the line itself as the message with unknown phase.
                # We don't know what the right phase is, use 'unknown'.
                # If it was a fatal error, presumably a 'failure'
                # message will arrive shortly.
                app_log.error("log event not json: %r", line)
                line = json.dumps(
                    {
                        "phase": "unknown",
                        "message": line,
                    }
                )

            self.progress(ProgressEvent.Kind.LOG_MESSAGE, line)
        else:
            app_log.info("Finished streaming logs of %s", self.name)

    def cleanup(self):
        """
        Delete the kubernetes build pod
        """
        try:
            self.api.delete_namespaced_pod(
                name=self.name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(grace_period_seconds=0),
                _request_timeout=KUBE_REQUEST_TIMEOUT,
            )
        except client.rest.ApiException as e:
            if e.status == 404:
                # Is ok, someone else has already deleted it
                pass
            else:
                raise


class KubernetesCleaner(LoggingConfigurable):
    """Regular cleanup utility for kubernetes builds

    Instantiate this class, and call cleanup() periodically.
    """

    kube = Any(help="kubernetes API client")

    @default("kube")
    def _default_kube(self):
        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.ConfigException:
            kubernetes.config.load_kube_config()
        return client.CoreV1Api()

    namespace = Unicode(help="Kubernetes namespace")

    @default("namespace")
    def _default_namespace(self):
        return os.getenv("BUILD_NAMESPACE", "default")

    max_age = Integer(
        3600 * 4,
        help="Maximum age of build pods to keep",
        config=True,
    )

    def cleanup(self):
        """Delete stopped build pods and build pods that have aged out"""
        builds = self.kube.list_namespaced_pod(
            namespace=self.namespace,
            label_selector="component=binderhub-build",
        ).items
        phases = defaultdict(int)
        app_log.debug("%i build pods", len(builds))
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        start_cutoff = now - datetime.timedelta(seconds=self.max_age)
        deleted = 0
        for build in builds:
            phase = build.status.phase
            phases[phase] += 1
            annotations = build.metadata.annotations or {}
            repo = annotations.get("binder-repo", "unknown")
            delete = False
            if build.status.phase in {"Failed", "Succeeded", "Evicted"}:
                # log Deleting Failed build build-image-...
                # print(build.metadata)
                app_log.info(
                    "Deleting %s build %s (repo=%s)",
                    build.status.phase,
                    build.metadata.name,
                    repo,
                )
                delete = True
            else:
                # check age
                started = build.status.start_time
                if self.max_age and started and started < start_cutoff:
                    app_log.info(
                        "Deleting long-running build %s (repo=%s)",
                        build.metadata.name,
                        repo,
                    )
                    delete = True

            if delete:
                deleted += 1
                try:
                    self.kube.delete_namespaced_pod(
                        name=build.metadata.name,
                        namespace=self.namespace,
                        body=client.V1DeleteOptions(grace_period_seconds=0),
                    )
                except client.rest.ApiException as e:
                    if e.status == 404:
                        # Is ok, someone else has already deleted it
                        pass
                    else:
                        raise

        if deleted:
            app_log.info("Deleted %i/%i build pods", deleted, len(builds))
        app_log.debug(
            "Build phase summary: %s", json.dumps(phases, sort_keys=True, indent=1)
        )


class FakeBuild(BuildExecutor):
    """
    Fake Building process to be able to work on the UI without a builder.
    """

    def submit(self):
        self.progress(
            ProgressEvent.Kind.BUILD_STATUS_CHANGE, ProgressEvent.BuildStatus.RUNNING
        )
        return

    def stream_logs(self):
        import time

        time.sleep(3)
        for phase in ("Pending", "Running", "Succeed", "Building"):
            if self.stop_event.is_set():
                app_log.warning("Stopping logs of %s", self.name)
                return
            self.progress(
                ProgressEvent.Kind.LOG_MESSAGE,
                json.dumps(
                    {
                        "phase": phase,
                        "message": f"{phase}...\n",
                    }
                ),
            )
        for i in range(5):
            if self.stop_event.is_set():
                app_log.warning("Stopping logs of %s", self.name)
                return
            time.sleep(1)
            self.progress(
                "log",
                json.dumps(
                    {
                        "phase": "unknown",
                        "message": f"Step {i+1}/10\n",
                    }
                ),
            )
        self.progress(
            ProgressEvent.Kind.BUILD_STATUS_CHANGE, ProgressEvent.BuildStatus.BUILT
        )
        self.progress(
            "log",
            json.dumps(
                {
                    "phase": "Deleted",
                    "message": "Deleted...\n",
                }
            ),
        )
