import time
from collections.abc import Callable
from typing import Any

from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.config.config_exception import ConfigException


class KubeOpsError(RuntimeError):
    pass


class KubeOps:
    def __init__(
        self,
        *,
        namespace: str,
        pod_selector: str,
        max_retries: int = 4,
        retry_base_delay_s: float = 2.0,
    ) -> None:
        self.namespace = namespace
        self.pod_selector = pod_selector
        self.max_retries = max_retries
        self.retry_base_delay_s = retry_base_delay_s

        try:
            config.load_kube_config()
        except ConfigException:
            try:
                config.load_incluster_config()
            except ConfigException as error:
                raise KubeOpsError(
                    "Unable to load Kubernetes config from kubeconfig or in-cluster config"
                ) from error

        self.api_client = client.ApiClient()
        self.core_api = client.CoreV1Api(self.api_client)
        self.apps_api = client.AppsV1Api(self.api_client)

    def _sanitize(self, payload: Any) -> Any:
        return self.api_client.sanitize_for_serialization(payload)

    def _is_retryable(self, error: Exception) -> bool:
        if isinstance(error, ApiException):
            return error.status in {429, 500, 502, 503, 504}
        lowered = str(error).lower()
        retryable_tokens = (
            "tls handshake timeout",
            "i/o timeout",
            "connection refused",
            "context deadline exceeded",
            "request canceled",
            "server is currently unable",
            "unexpected eof",
            "temporarily unavailable",
        )
        return any(token in lowered for token in retryable_tokens)

    def _with_retries(self, operation: str, fn: Callable[[], Any]) -> Any:
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except ApiException as error:
                if error.status == 404:
                    raise
                if self._is_retryable(error) and attempt < attempts:
                    time.sleep(self.retry_base_delay_s * attempt)
                    continue
                raise KubeOpsError(
                    f"{operation} failed (status={error.status}): {error.reason}"
                ) from error
            except Exception as error:  # noqa: BLE001
                if self._is_retryable(error) and attempt < attempts:
                    time.sleep(self.retry_base_delay_s * attempt)
                    continue
                raise KubeOpsError(f"{operation} failed: {error}") from error
        raise KubeOpsError(f"{operation} failed after retries")

    def list_pods(self) -> list[dict[str, Any]]:
        def _call() -> Any:
            return self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=self.pod_selector,
                _request_timeout=60,
            )

        response = self._with_retries("list pods", _call)
        return [self._sanitize(item) for item in response.items]

    def read_pod(self, pod_name: str) -> dict[str, Any]:
        def _call() -> Any:
            return self.core_api.read_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                _request_timeout=60,
            )

        pod = self._with_retries(f"read pod {pod_name}", _call)
        return self._sanitize(pod)

    def try_read_pod(self, pod_name: str) -> dict[str, Any] | None:
        try:
            return self.read_pod(pod_name)
        except ApiException as error:
            if error.status == 404:
                return None
            raise KubeOpsError(
                f"read pod {pod_name} failed (status={error.status}): {error.reason}"
            ) from error

    def read_pvc(self, pvc_name: str) -> dict[str, Any]:
        def _call() -> Any:
            return self.core_api.read_namespaced_persistent_volume_claim(
                name=pvc_name,
                namespace=self.namespace,
                _request_timeout=60,
            )

        pvc = self._with_retries(f"read pvc {pvc_name}", _call)
        return self._sanitize(pvc)

    def list_nodes(self) -> list[dict[str, Any]]:
        def _call() -> Any:
            return self.core_api.list_node(_request_timeout=60)

        response = self._with_retries("list nodes", _call)
        return [self._sanitize(item) for item in response.items]

    def scale_deployment(self, deployment_name: str, replicas: int) -> None:
        patch = {"spec": {"replicas": replicas}}

        def _call() -> Any:
            return self.apps_api.patch_namespaced_deployment_scale(
                name=deployment_name,
                namespace=self.namespace,
                body=patch,
                _request_timeout=60,
            )

        self._with_retries(
            f"scale deployment {deployment_name} to {replicas}",
            _call,
        )

    def create_pod(self, manifest: dict[str, Any]) -> dict[str, Any]:
        def _call() -> Any:
            return self.core_api.create_namespaced_pod(
                namespace=self.namespace,
                body=manifest,
                _request_timeout=60,
            )

        pod = self._with_retries("create pod", _call)
        return self._sanitize(pod)

    def delete_pod(self, pod_name: str, *, grace_period_seconds: int = 0) -> None:
        body = client.V1DeleteOptions(grace_period_seconds=grace_period_seconds)

        def _call() -> Any:
            return self.core_api.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                body=body,
                _request_timeout=60,
            )

        try:
            self._with_retries(f"delete pod {pod_name}", _call)
        except ApiException as error:
            if error.status == 404:
                return
            raise KubeOpsError(
                f"delete pod {pod_name} failed (status={error.status}): {error.reason}"
            ) from error

    def read_pod_log(self, pod_name: str, container_name: str | None = None) -> str:
        def _call() -> Any:
            return self.core_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=self.namespace,
                container=container_name,
                timestamps=True,
                _request_timeout=300,
            )

        return str(
            self._with_retries(
                f"read pod log {pod_name} container={container_name or '<default>'}",
                _call,
            )
        )

    def read_all_pod_logs(self, pod_name: str) -> str:
        pod = self.read_pod(pod_name)
        spec = pod.get("spec") or {}
        container_names: list[str] = []
        for container in spec.get("initContainers") or []:
            name = container.get("name")
            if name:
                container_names.append(str(name))
        for container in spec.get("containers") or []:
            name = container.get("name")
            if name and name not in container_names:
                container_names.append(str(name))

        if not container_names:
            return ""

        sections: list[str] = []
        for container_name in container_names:
            try:
                logs = self.read_pod_log(pod_name, container_name)
            except KubeOpsError as error:
                logs = f"error: {error}"
            sections.append(f"===== container: {container_name} =====\n{logs}")
        return "\n\n".join(sections).strip()

    def list_events_for_pod(self, pod_name: str) -> dict[str, Any]:
        def _call() -> Any:
            return self.core_api.list_namespaced_event(
                namespace=self.namespace,
                field_selector=f"involvedObject.name={pod_name}",
                _request_timeout=60,
            )

        response = self._with_retries(f"list events for pod {pod_name}", _call)
        return {"items": [self._sanitize(item) for item in response.items]}
