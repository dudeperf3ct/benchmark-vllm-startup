import argparse
import csv
import http.client
import json
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.kube_ops import KubeOps, KubeOpsError

NAMESPACE = "llm-bench"
DEPLOYMENT = "vllm-baseline"
SERVICE = "vllm-baseline"
POD_SELECTOR = "app=vllm-baseline"

PROMPT = "Explain in 3 concise bullet points what cold start latency means for LLM inference."
MAX_TOKENS = 64
TEMPERATURE = 0.0
TOP_P = 1.0
SEED = 0

KUBECTL_TIMEOUT_S = 120
POD_START_TIMEOUT_S = 1800
HEALTH_TIMEOUT_S = 1800
MODEL_ID_TIMEOUT_S = 120
CACHE_CLEAR_TIMEOUT_S = 1200
STATUS_LOG_INTERVAL_S = 30
PORT_FORWARD_RESTART_DELAY_S = 1
KUBECTL_TRANSIENT_RETRIES = 4
KUBECTL_RETRY_BASE_DELAY_S = 2
HEALTH_ENDPOINT = "/health"
MODELS_ENDPOINT = "/v1/models"

RETRYABLE_KUBECTL_ERROR_TOKENS = (
    "tls handshake timeout",
    "i/o timeout",
    "connection refused",
    "context deadline exceeded",
    "client rate limiter wait",
    "request canceled",
    "server is currently unable",
    "unexpected eof",
)

PollOutcome = tuple[bool, str, Any]
PortForwardPoll = Callable[[int], PollOutcome]

SESSION_LOG_PATH: Path | None = None
KUBE_OPS: KubeOps | None = None

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results"
RAW_RESULTS_DIR = RESULTS_DIR / "raw"
RESULTS_CSV = RESULTS_DIR / "results.csv"

CSV_COLUMNS = [
    "run_id",
    "variant",
    "run_kind",
    "status",
    "started_at_utc",
    "t0_utc",
    "container_started_at_utc",
    "pod_name",
    "node_name",
    "image_streaming_state",
    "vllm_image_ref",
    "vllm_image_pull_state",
    "vllm_image_pull_duration_s",
    "vllm_image_pull_reported_s",
    "init_image_ref",
    "init_image_pull_state",
    "init_image_pull_duration_s",
    "init_image_pull_reported_s",
    "scale_to_container_start_s",
    "container_start_to_health_s",
    "deploy_to_health_s",
    "health_to_first_request_s",
    "deploy_to_first_request_s",
    "deploy_to_first_token_s",
    "request_to_first_token_s",
    "request_to_done_s",
    "error_stage",
    "error_message",
]


@dataclass(frozen=True)
class VariantConfig:
    cache_pvcs: tuple[str, ...]
    expected_image_streaming: bool
    extra_apply_files: tuple[str, ...] = ()
    config_patch_file: str | None = None
    deployment_patch_file: str | None = None


BASELINE_APPLY_FILES = (
    "k8s/baseline/namespace.yaml",
    "k8s/baseline/benchmark-config.yaml",
    "k8s/baseline/pvc.yaml",
    "k8s/baseline/service.yaml",
    "k8s/baseline/deployment.yaml",
)


VARIANTS: dict[str, VariantConfig] = {
    "v0": VariantConfig(
        cache_pvcs=("vllm-hf-cache",),
        expected_image_streaming=False,
    ),
    "v1": VariantConfig(
        cache_pvcs=("vllm-hf-cache",),
        expected_image_streaming=False,
    ),
    "v2": VariantConfig(
        cache_pvcs=("vllm-hf-cache", "vllm-model-store"),
        expected_image_streaming=False,
        extra_apply_files=("k8s/variants/object-storage-baseline/pvc.yaml",),
        config_patch_file="k8s/variants/object-storage-baseline/benchmark-config-patch.yaml",
        deployment_patch_file="k8s/variants/object-storage-baseline/deployment-patch.yaml",
    ),
    "v3": VariantConfig(
        cache_pvcs=("vllm-hf-cache", "vllm-model-store"),
        expected_image_streaming=True,
        extra_apply_files=("k8s/variants/object-storage-baseline/pvc.yaml",),
        config_patch_file="k8s/variants/object-storage-baseline/benchmark-config-patch.yaml",
        deployment_patch_file="k8s/variants/object-storage-baseline/deployment-patch.yaml",
    ),
    "v4": VariantConfig(
        cache_pvcs=("vllm-hf-cache",),
        expected_image_streaming=True,
        config_patch_file="k8s/variants/runai-streamer/benchmark-config-patch.yaml",
        deployment_patch_file="k8s/variants/runai-streamer/deployment-patch.yaml",
    ),
    "v5": VariantConfig(
        cache_pvcs=("vllm-hf-cache",),
        expected_image_streaming=False,
        config_patch_file="k8s/variants/runai-streamer/benchmark-config-patch.yaml",
        deployment_patch_file="k8s/variants/runai-streamer/deployment-patch.yaml",
    ),
}


class BenchmarkError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def init_kube_ops() -> None:
    global KUBE_OPS
    try:
        KUBE_OPS = KubeOps(
            namespace=NAMESPACE,
            pod_selector=POD_SELECTOR,
            max_retries=KUBECTL_TRANSIENT_RETRIES,
            retry_base_delay_s=KUBECTL_RETRY_BASE_DELAY_S,
        )
    except KubeOpsError as error:
        raise BenchmarkError("kubernetes", str(error)) from error


def kube_ops() -> KubeOps:
    if KUBE_OPS is None:
        raise BenchmarkError("kubernetes", "Kubernetes client is not initialized")
    return KUBE_OPS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one constrained benchmark pass (cold + warm) for a variant."
    )
    parser.add_argument("--variant", choices=sorted(VARIANTS.keys()), required=True)
    return parser.parse_args()


def run_kubectl(
    args: list[str],
    *,
    timeout: int = KUBECTL_TIMEOUT_S,
    check: bool = True,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["kubectl", *args]
    attempts = KUBECTL_TRANSIENT_RETRIES + 1 if check else 1

    for attempt in range(1, attempts + 1):
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=stdin,
            )
        except subprocess.TimeoutExpired as error:
            if check and attempt < attempts:
                delay = KUBECTL_RETRY_BASE_DELAY_S * attempt
                log(
                    f"Retrying kubectl after timeout (attempt {attempt}/{attempts}) "
                    f"for {' '.join(command)} in {delay}s"
                )
                time.sleep(delay)
                continue
            raise BenchmarkError(
                "kubectl",
                f"{' '.join(command)} timed out after {timeout}s: {one_line(str(error))}",
            ) from error

        if completed.returncode == 0 or not check:
            return completed

        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or f"kubectl exited with {completed.returncode}"
        lowered = message.lower()
        retryable = any(token in lowered for token in RETRYABLE_KUBECTL_ERROR_TOKENS)

        if retryable and attempt < attempts:
            delay = KUBECTL_RETRY_BASE_DELAY_S * attempt
            log(
                f"Retrying kubectl after transient error (attempt {attempt}/{attempts}) "
                f"for {' '.join(command)} in {delay}s: {one_line(message)}"
            )
            time.sleep(delay)
            continue

        raise BenchmarkError("kubectl", f"{' '.join(command)} failed: {message}")

    raise BenchmarkError("kubectl", f"{' '.join(command)} failed after retries")


def resolve_repo_file(relative_path: str) -> Path:
    path = (REPO_ROOT / relative_path).resolve()
    if not path.exists():
        raise BenchmarkError("variant", f"Missing file: {path}")
    return path


def apply_manifest_file(relative_path: str) -> None:
    path = resolve_repo_file(relative_path)
    run_kubectl(["apply", "-f", str(path)], timeout=300)


def patch_resource(kind: str, name: str, patch_relative_path: str) -> None:
    patch_path = resolve_repo_file(patch_relative_path)
    run_kubectl(
        [
            "-n",
            NAMESPACE,
            "patch",
            kind,
            name,
            "--type=strategic",
            "--patch-file",
            str(patch_path),
        ],
        timeout=300,
    )


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def log(message: str) -> None:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    if SESSION_LOG_PATH is not None:
        with SESSION_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def set_session_log(path: Path | None) -> None:
    global SESSION_LOG_PATH
    SESSION_LOG_PATH = path


def one_line(value: str) -> str:
    return " ".join(value.split())


def format_seconds(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def parse_k8s_time(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


LOG_TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


def extract_log_insights(
    logs: str, *, container_started_at_utc: str = ""
) -> dict[str, Any]:
    container_started_at = parse_k8s_time(container_started_at_utc)
    model_start_at = None
    model_loaded_at = None
    compile_done_at = None
    graph_done_at = None
    model_loading_s = None
    remote_model_download_s = None
    local_weight_loading_s = None
    torch_compile_s = None
    cuda_graph_capture_s = None
    init_engine_s = None

    for line in logs.splitlines():
        ts_match = LOG_TIMESTAMP_RE.match(line)
        line_ts = parse_k8s_time(ts_match.group(1)) if ts_match else None

        if "Starting to load model " in line and line_ts and model_start_at is None:
            model_start_at = line_ts

        model_match = re.search(
            r"Model loading took [^\n]* and ([0-9.]+) seconds", line
        )
        if model_match:
            model_loading_s = float(model_match.group(1))
            if line_ts:
                model_loaded_at = line_ts

        remote_download_match = re.search(
            r"Time spent downloading weights for [^\n]*: ([0-9.]+) seconds", line
        )
        if remote_download_match:
            remote_model_download_s = float(remote_download_match.group(1))

        local_weight_match = re.search(r"Loading weights took ([0-9.]+) seconds", line)
        if local_weight_match:
            local_weight_loading_s = float(local_weight_match.group(1))

        compile_match = re.search(r"torch\.compile took ([0-9.]+) s in total", line)
        if compile_match:
            torch_compile_s = float(compile_match.group(1))
            if line_ts:
                compile_done_at = line_ts

        graph_match = re.search(r"Graph capturing finished in ([0-9.]+) secs", line)
        if graph_match:
            cuda_graph_capture_s = float(graph_match.group(1))
            if line_ts:
                graph_done_at = line_ts

        init_match = re.search(
            r"init engine \(profile, create kv cache, warmup model\) took ([0-9.]+) seconds",
            line,
        )
        if init_match:
            init_engine_s = float(init_match.group(1))

    boot_to_model_start_s = None
    if container_started_at and model_start_at:
        boot_to_model_start_s = (model_start_at - container_started_at).total_seconds()

    post_model_to_compile_done_s = None
    if model_loaded_at and compile_done_at:
        post_model_to_compile_done_s = (
            compile_done_at - model_loaded_at
        ).total_seconds()

    compile_done_to_graph_done_s = None
    if compile_done_at and graph_done_at:
        compile_done_to_graph_done_s = (graph_done_at - compile_done_at).total_seconds()

    model_loading_other_s = None
    if model_loading_s is not None:
        model_loading_other_s = (
            model_loading_s
            - (remote_model_download_s or 0)
            - (local_weight_loading_s or 0)
        )

    return {
        "boot_to_model_start_s": format_seconds(boot_to_model_start_s),
        "model_loading_s": format_seconds(model_loading_s),
        "remote_model_download_s": format_seconds(remote_model_download_s),
        "local_weight_loading_s": format_seconds(local_weight_loading_s),
        "model_loading_other_s": format_seconds(model_loading_other_s),
        "torch_compile_s": format_seconds(torch_compile_s),
        "cuda_graph_capture_s": format_seconds(cuda_graph_capture_s),
        "init_engine_s": format_seconds(init_engine_s),
        "post_model_to_compile_done_s": format_seconds(post_model_to_compile_done_s),
        "compile_done_to_graph_done_s": format_seconds(compile_done_to_graph_done_s),
        "model_start_at_utc": model_start_at.isoformat() if model_start_at else "",
        "model_loaded_at_utc": model_loaded_at.isoformat() if model_loaded_at else "",
        "compile_done_at_utc": compile_done_at.isoformat() if compile_done_at else "",
        "graph_done_at_utc": graph_done_at.isoformat() if graph_done_at else "",
    }


def list_benchmark_pods() -> list[dict[str, Any]]:
    try:
        return kube_ops().list_pods()
    except KubeOpsError as error:
        raise BenchmarkError("kubernetes", str(error)) from error


def get_image_streaming_state() -> str:
    try:
        nodes = kube_ops().list_nodes()
    except KubeOpsError:
        return "unknown"

    if not nodes:
        return "unknown"

    states: list[bool] = []
    for node in nodes:
        labels = node.get("metadata", {}).get("labels", {})
        value = labels.get("cloud.google.com/gke-image-streaming")
        if value is None:
            states.append(False)
            continue
        normalized = str(value).strip().lower()
        states.append(normalized in {"true", "1", "enabled", "yes"})

    if all(states):
        return "enabled"
    if not any(states):
        return "disabled"
    return "mixed"


def validate_variant_image_streaming(variant: str) -> str:
    expected = VARIANTS[variant].expected_image_streaming
    observed = get_image_streaming_state()

    if observed == "unknown":
        raise BenchmarkError(
            "variant",
            (
                "unable to verify node image streaming state from Kubernetes API. "
                "Fix cluster access and retry."
            ),
        )

    if expected and observed != "enabled":
        raise BenchmarkError(
            "variant",
            (
                f"{variant} expects image streaming enabled but observed '{observed}'. "
                "Enable GKE image streaming for this run block."
            ),
        )

    if not expected and observed != "disabled":
        raise BenchmarkError(
            "variant",
            (
                f"{variant} expects image streaming disabled but observed '{observed}'. "
                "Disable GKE image streaming for this run block."
            ),
        )

    return observed


def scale_deployment(replicas: int) -> None:
    try:
        kube_ops().scale_deployment(DEPLOYMENT, replicas)
    except KubeOpsError as error:
        raise BenchmarkError("kubernetes", str(error)) from error


def wait_for_no_pods(timeout_s: int = 600) -> None:
    deadline = time.monotonic() + timeout_s
    last_log_at = 0.0
    while time.monotonic() < deadline:
        pods = list_benchmark_pods()
        if not pods:
            return
        now = time.monotonic()
        if now - last_log_at >= STATUS_LOG_INTERVAL_S:
            names = [pod.get("metadata", {}).get("name", "<unknown>") for pod in pods]
            log(f"Waiting for pods to terminate: {names}")
            last_log_at = now
        time.sleep(2)
    names = [
        pod.get("metadata", {}).get("name", "<unknown>")
        for pod in list_benchmark_pods()
    ]
    raise BenchmarkError(
        "scale_down", f"Timed out waiting for pods to terminate: {names}"
    )


def select_newest_pod(pods: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pods:
        return None
    return sorted(
        pods,
        key=lambda pod: pod.get("metadata", {}).get("creationTimestamp", ""),
    )[-1]


def wait_for_pod_container_start(previous_pods: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + POD_START_TIMEOUT_S
    last_status = ""
    last_log_at = 0.0
    while time.monotonic() < deadline:
        pods = list_benchmark_pods()
        candidates = [
            pod
            for pod in pods
            if pod.get("metadata", {}).get("name") not in previous_pods
        ]
        pod = select_newest_pod(candidates or pods)
        if pod is None:
            time.sleep(2)
            continue

        name = pod.get("metadata", {}).get("name", "<unknown>")
        phase = pod.get("status", {}).get("phase", "Unknown")
        last_status = f"pod={name} phase={phase}"
        if phase == "Failed":
            raise BenchmarkError(
                "pod_start", f"Pod failed before starting container: {name}"
            )

        statuses = pod.get("status", {}).get("containerStatuses") or []
        vllm = next(
            (status for status in statuses if status.get("name") == "vllm"), None
        )
        if vllm is None and statuses:
            vllm = statuses[0]
        if vllm:
            running = vllm.get("state", {}).get("running")
            if running and running.get("startedAt"):
                return pod
            waiting = vllm.get("state", {}).get("waiting")
            terminated = vllm.get("state", {}).get("terminated")
            if waiting and waiting.get("reason"):
                last_status = f"{last_status} waiting={waiting.get('reason')}"
            if terminated and terminated.get("reason"):
                last_status = f"{last_status} terminated={terminated.get('reason')}"

        now = time.monotonic()
        if now - last_log_at >= STATUS_LOG_INTERVAL_S:
            log(f"Waiting for pod container start: {last_status}")
            last_log_at = now
        time.sleep(2)

    raise BenchmarkError(
        "pod_start_timeout",
        f"Timed out waiting for container start. Last status: {last_status}",
    )


def pick_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_port_forward(local_port: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "port-forward",
            f"service/{SERVICE}",
            f"{local_port}:8000",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def stop_process(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is None:
        return ""
    return process.stdout.read().strip()


def http_get(local_port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", local_port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, body
    finally:
        connection.close()


def poll_health_once(local_port: int) -> PollOutcome:
    status, body = http_get(local_port, HEALTH_ENDPOINT)
    if status == 200:
        return True, "", time.monotonic()
    return (
        False,
        f"HTTP {status}: {body.decode('utf-8', errors='replace')}",
        None,
    )


def poll_model_id_once(local_port: int) -> PollOutcome:
    status, body = http_get(local_port, MODELS_ENDPOINT)
    if status != 200:
        return (
            False,
            f"HTTP {status}: {body.decode('utf-8', errors='replace')}",
            None,
        )

    payload = json.loads(body.decode("utf-8", errors="replace"))
    models = payload.get("data") or []
    if models and models[0].get("id"):
        return True, "", str(models[0]["id"])
    return False, "No model ID returned from /v1/models", None


def wait_for_port_forward_endpoint(
    local_port: int,
    process: subprocess.Popen[str],
    *,
    timeout_s: int,
    poll_once: PortForwardPoll,
    waiting_log_message: str,
    restart_log_context: str,
    timeout_stage: str,
    timeout_context: str,
) -> tuple[Any, subprocess.Popen[str]]:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    restart_count = 0
    last_log_at = 0.0

    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = stop_process(process)
            restart_count += 1
            last_error = f"port-forward exited: {one_line(output)}"
            log(
                f"port-forward exited during {restart_log_context} "
                f"(restart {restart_count}): {one_line(output)}"
            )
            process = start_port_forward(local_port)
            time.sleep(PORT_FORWARD_RESTART_DELAY_S)
            continue

        try:
            done, error_message, value = poll_once(local_port)
            if done:
                return value, process
            last_error = error_message
        except (OSError, json.JSONDecodeError) as error:
            last_error = str(error)

        now = time.monotonic()
        if now - last_log_at >= STATUS_LOG_INTERVAL_S:
            log(f"{waiting_log_message}. Last error: {one_line(last_error)}")
            last_log_at = now
        time.sleep(1)

    raise BenchmarkError(
        timeout_stage,
        (
            f"Timed out waiting for {timeout_context} after {restart_count} "
            f"port-forward restart(s). Last error: {one_line(last_error)}"
        ),
    )


def wait_for_health(
    local_port: int, process: subprocess.Popen[str]
) -> tuple[float, subprocess.Popen[str]]:
    value, next_process = wait_for_port_forward_endpoint(
        local_port,
        process,
        timeout_s=HEALTH_TIMEOUT_S,
        poll_once=poll_health_once,
        waiting_log_message="Waiting for /health to become ready",
        restart_log_context="health wait",
        timeout_stage="health_timeout",
        timeout_context=HEALTH_ENDPOINT,
    )
    return float(value), next_process


def wait_for_model_id(
    local_port: int, process: subprocess.Popen[str]
) -> tuple[str, subprocess.Popen[str]]:
    value, next_process = wait_for_port_forward_endpoint(
        local_port,
        process,
        timeout_s=MODEL_ID_TIMEOUT_S,
        poll_once=poll_model_id_once,
        waiting_log_message="Waiting for /v1/models response",
        restart_log_context="/v1/models wait",
        timeout_stage="model_id_timeout",
        timeout_context=MODELS_ENDPOINT,
    )
    return str(value), next_process


def send_streaming_request(
    local_port: int, model_id: str
) -> tuple[float, float, float]:
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "seed": SEED,
    }
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}

    connection = http.client.HTTPConnection("127.0.0.1", local_port, timeout=300)
    t3 = time.monotonic()
    connection.request("POST", "/v1/chat/completions", body=body, headers=headers)
    response = connection.getresponse()
    if response.status != 200:
        message = response.read(1024).decode("utf-8", errors="replace")
        connection.close()
        raise BenchmarkError(
            "request_http",
            f"/v1/chat/completions returned {response.status}: {one_line(message)}",
        )

    first_chunk_time: float | None = None
    while True:
        line = response.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text or not text.startswith("data:"):
            continue
        data = text[5:].strip()
        if data == "[DONE]":
            break
        if first_chunk_time is None:
            first_chunk_time = time.monotonic()

    done_time = time.monotonic()
    connection.close()
    if first_chunk_time is None:
        raise BenchmarkError(
            "first_token", "No streamed chunk received before stream ended"
        )
    return t3, first_chunk_time, done_time


def clear_cache_pvc(pvc_name: str) -> None:
    try:
        kube = kube_ops()
        kube.read_pvc(pvc_name)
    except KubeOpsError as error:
        raise BenchmarkError("cache_reset", str(error)) from error

    pod_name = f"cache-clear-{pvc_name}-{time.time_ns()}".replace("_", "-")
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": NAMESPACE},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "clear",
                    "image": "busybox:1.36",
                    "command": [
                        "sh",
                        "-lc",
                        "rm -rf /cache/* /cache/.[!.]* /cache/..?* || true",
                    ],
                    "volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
                }
            ],
            "volumes": [
                {"name": "cache", "persistentVolumeClaim": {"claimName": pvc_name}}
            ],
        },
    }

    try:
        try:
            kube.create_pod(manifest)
        except KubeOpsError as error:
            raise BenchmarkError("cache_reset", str(error)) from error

        deadline = time.monotonic() + CACHE_CLEAR_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                payload = kube.try_read_pod(pod_name)
            except KubeOpsError as error:
                raise BenchmarkError("cache_reset", str(error)) from error

            if payload is None:
                time.sleep(2)
                continue

            phase = payload.get("status", {}).get("phase")
            if phase == "Succeeded":
                return
            if phase == "Failed":
                try:
                    logs = kube.read_pod_log(pod_name, "clear")
                except KubeOpsError as error:
                    logs = f"error reading cache-clear logs: {error}"
                raise BenchmarkError(
                    "cache_reset",
                    f"Cache clear pod failed for pvc={pvc_name}: {one_line(logs)}",
                )
            time.sleep(2)
        raise BenchmarkError("cache_reset", f"Timed out clearing PVC: {pvc_name}")
    finally:
        try:
            kube.delete_pod(pod_name, grace_period_seconds=0)
        except KubeOpsError:
            pass


def append_csv_row(row: dict[str, str]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = RESULTS_CSV.exists()
    with RESULTS_CSV.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def event_timestamp(item: dict[str, Any]) -> datetime | None:
    for key in ("eventTime", "lastTimestamp", "firstTimestamp"):
        parsed = parse_k8s_time(item.get(key))
        if parsed is not None:
            return parsed
    metadata = item.get("metadata") or {}
    return parse_k8s_time(metadata.get("creationTimestamp"))


def parse_reported_duration_seconds(message: str) -> float | None:
    match = re.search(r"in (?:(\d+)m)?([0-9]+(?:\.[0-9]+)?)s", message)
    if not match:
        return None
    minutes = int(match.group(1) or "0")
    seconds = float(match.group(2))
    return minutes * 60 + seconds


def parse_container_field_path(field_path: str) -> tuple[str, str] | None:
    match = re.match(r"spec\.(initContainers|containers)\{([^}]+)\}", field_path)
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_image_ref_from_event_message(message: str) -> str:
    match = re.search(r'image\s+"([^"]+)"', message, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


def summarize_pull_events(
    pulling: tuple[datetime, str, str] | None,
    pulled: tuple[datetime, str, str] | None,
) -> dict[str, str]:
    state = "unknown"
    if pulled is not None:
        lowered = pulled[1].lower()
        if "already present on machine" in lowered:
            state = "already_present"
        else:
            state = "pulled"
    elif pulling is not None:
        state = "pulling_only"

    duration = ""
    if pulling and pulled:
        duration = format_seconds((pulled[0] - pulling[0]).total_seconds())

    reported = ""
    if pulled:
        parsed = parse_reported_duration_seconds(pulled[1])
        if parsed is not None:
            reported = format_seconds(parsed)

    image_ref = ""
    if pulled and pulled[2]:
        image_ref = pulled[2]
    elif pulling and pulling[2]:
        image_ref = pulling[2]

    return {
        "image_ref": image_ref,
        "pulling_at_utc": pulling[0].isoformat() if pulling else "",
        "pulled_at_utc": pulled[0].isoformat() if pulled else "",
        "pull_duration_s": duration,
        "pull_reported_s": reported,
        "pull_state": state,
    }


def extract_event_insights(events_payload: dict[str, Any]) -> dict[str, Any]:
    items = events_payload.get("items") or []
    enriched = [(event_timestamp(item), item) for item in items]
    enriched.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC))

    pulling: tuple[datetime, str, str] | None = None
    pulled: tuple[datetime, str, str] | None = None
    vllm_pulling: tuple[datetime, str, str] | None = None
    vllm_pulled: tuple[datetime, str, str] | None = None
    init_pulling: tuple[datetime, str, str] | None = None
    init_pulled: tuple[datetime, str, str] | None = None
    scheduled: datetime | None = None
    started_container: datetime | None = None

    for ts, item in enriched:
        if ts is None:
            continue
        reason = str(item.get("reason", ""))
        message = one_line(str(item.get("message", "")))
        involved = item.get("involvedObject") or {}
        field_path = str(involved.get("fieldPath") or "")
        image_ref = parse_image_ref_from_event_message(message)

        if scheduled is None and reason == "Scheduled":
            scheduled = ts

        if started_container is None and reason == "Started" and "container" in message:
            started_container = ts

        if pulling is None and reason == "Pulling":
            pulling = (ts, message, image_ref)

        if pulled is None and reason == "Pulled":
            pulled = (ts, message, image_ref)

        parsed = parse_container_field_path(field_path)
        if parsed is None:
            continue

        container_group, container_name = parsed
        if container_group == "containers" and container_name == "vllm":
            if reason == "Pulling" and vllm_pulling is None:
                vllm_pulling = (ts, message, image_ref)
            if reason == "Pulled" and vllm_pulled is None:
                vllm_pulled = (ts, message, image_ref)
        elif container_group == "initContainers":
            if reason == "Pulling" and init_pulling is None:
                init_pulling = (ts, message, image_ref)
            if reason == "Pulled" and init_pulled is None:
                init_pulled = (ts, message, image_ref)

    generic_pull = summarize_pull_events(pulling, pulled)
    vllm_pull = summarize_pull_events(vllm_pulling, vllm_pulled)
    init_pull = summarize_pull_events(init_pulling, init_pulled)

    insights: dict[str, Any] = {
        "event_count": len(items),
        "scheduled_at_utc": scheduled.isoformat() if scheduled else "",
        "container_started_event_utc": started_container.isoformat()
        if started_container
        else "",
        "image_pulling_at_utc": generic_pull["pulling_at_utc"],
        "image_pulling_message": pulling[1] if pulling else "",
        "image_pulled_at_utc": generic_pull["pulled_at_utc"],
        "image_pulled_message": pulled[1] if pulled else "",
        "image_pull_duration_s": generic_pull["pull_duration_s"],
        "image_pull_reported_s": generic_pull["pull_reported_s"],
        "image_already_present": str(
            generic_pull["pull_state"] == "already_present"
        ).lower(),
        "vllm_image_ref": vllm_pull["image_ref"],
        "vllm_image_pulling_at_utc": vllm_pull["pulling_at_utc"],
        "vllm_image_pulled_at_utc": vllm_pull["pulled_at_utc"],
        "vllm_image_pull_duration_s": vllm_pull["pull_duration_s"],
        "vllm_image_pull_reported_s": vllm_pull["pull_reported_s"],
        "vllm_image_pull_state": vllm_pull["pull_state"],
        "init_image_ref": init_pull["image_ref"],
        "init_image_pulling_at_utc": init_pull["pulling_at_utc"],
        "init_image_pulled_at_utc": init_pull["pulled_at_utc"],
        "init_image_pull_duration_s": init_pull["pull_duration_s"],
        "init_image_pull_reported_s": init_pull["pull_reported_s"],
        "init_image_pull_state": init_pull["pull_state"],
    }

    return insights


def capture_pod_artifacts(
    run_dir: Path, pod_name: str, *, container_started_at_utc: str = ""
) -> dict[str, Any]:
    artifact_summary: dict[str, Any] = {}
    kube = kube_ops()

    try:
        pod_payload = kube.read_pod(pod_name)
        write_json(run_dir / "pod.json", pod_payload)
        artifact_summary["pod_phase"] = pod_payload.get("status", {}).get("phase", "")
    except KubeOpsError as error:
        write_json(
            run_dir / "pod.json",
            {"error": one_line(str(error))},
        )

    try:
        events_payload = kube.list_events_for_pod(pod_name)
        write_json(run_dir / "events.json", events_payload)
        event_insights = extract_event_insights(events_payload)
        write_json(run_dir / "event-insights.json", event_insights)
        artifact_summary["event_insights"] = event_insights
    except KubeOpsError as error:
        write_json(
            run_dir / "events.json",
            {"error": one_line(str(error))},
        )

    try:
        logs = kube.read_all_pod_logs(pod_name)
        (run_dir / "pod-logs.txt").write_text(logs, encoding="utf-8")
        log_insights = extract_log_insights(
            logs, container_started_at_utc=container_started_at_utc
        )
        write_json(run_dir / "log-insights.json", log_insights)
        artifact_summary["log_insights"] = log_insights
    except KubeOpsError as error:
        (run_dir / "pod-logs.txt").write_text(
            f"error={one_line(str(error))}\n",
            encoding="utf-8",
        )
        write_json(run_dir / "log-insights.json", {"error": one_line(str(error))})

    describe = run_kubectl(
        ["-n", NAMESPACE, "describe", "pod", pod_name],
        check=False,
        timeout=300,
    )
    if describe.returncode == 0:
        (run_dir / "pod-describe.txt").write_text(describe.stdout, encoding="utf-8")
    else:
        (run_dir / "pod-describe.txt").write_text(
            f"error={one_line(describe.stderr or describe.stdout)}\n",
            encoding="utf-8",
        )

    return artifact_summary


def create_run_id(variant: str, run_kind: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}-{time.time_ns()}-{variant}-{run_kind}"


def run_single_iteration(
    variant: str,
    run_kind: str,
    clear_cache: bool,
    image_streaming_state: str,
    session_dir: Path,
) -> dict[str, str]:
    run_id = create_run_id(variant, run_kind)
    run_dir = RAW_RESULTS_DIR / run_id
    session_run_dir = session_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    session_run_dir.mkdir(parents=True, exist_ok=True)

    row = {column: "" for column in CSV_COLUMNS}
    row["run_id"] = run_id
    row["variant"] = variant
    row["run_kind"] = run_kind
    row["status"] = "failed"
    row["started_at_utc"] = now_utc_iso()
    row["image_streaming_state"] = image_streaming_state

    stage = "init"
    t0_monotonic: float | None = None
    t2_monotonic: float | None = None
    t3_monotonic: float | None = None
    t4_monotonic: float | None = None
    t5_monotonic: float | None = None
    port_forward: subprocess.Popen[str] | None = None
    pod_name = ""
    metadata: dict[str, Any] = {
        "run_id": run_id,
        "variant": variant,
        "run_kind": run_kind,
        "clear_cache": clear_cache,
        "image_streaming_state": image_streaming_state,
        "session_run_dir": str(session_run_dir),
        "timestamps": {},
    }

    try:
        stage = "scale_down"
        log(f"{run_id}: stage={stage} (scaling deployment to 0)")
        scale_deployment(0)
        wait_for_no_pods()

        if clear_cache:
            stage = "cache_reset"
            log(f"{run_id}: stage={stage} (clearing cache PVCs)")
            for pvc_name in VARIANTS[variant].cache_pvcs:
                log(f"{run_id}: clearing PVC {pvc_name}")
                clear_cache_pvc(pvc_name)

        previous_pods = {
            pod.get("metadata", {}).get("name", "") for pod in list_benchmark_pods()
        }
        stage = "scale_up"
        log(f"{run_id}: stage={stage} (scaling deployment to 1)")
        t0_wall = datetime.now(UTC)
        t0_monotonic = time.monotonic()
        metadata["timestamps"]["t0_utc"] = t0_wall.isoformat()
        row["t0_utc"] = t0_wall.isoformat()
        scale_deployment(1)

        stage = "wait_pod"
        log(f"{run_id}: stage={stage} (waiting for container start)")
        pod = wait_for_pod_container_start(previous_pods)
        pod_name = pod.get("metadata", {}).get("name", "")
        row["pod_name"] = pod_name
        row["node_name"] = pod.get("spec", {}).get("nodeName", "")
        log(
            f"{run_id}: pod started pod_name={row['pod_name']} node_name={row['node_name']}"
        )
        statuses = pod.get("status", {}).get("containerStatuses") or []
        vllm = next(
            (status for status in statuses if status.get("name") == "vllm"), None
        )
        started_at = (vllm or {}).get("state", {}).get("running", {}).get("startedAt")
        if started_at:
            row["container_started_at_utc"] = started_at
            started_dt = parse_k8s_time(started_at)
            if started_dt is not None:
                metadata["timestamps"]["container_started_at_utc"] = (
                    started_dt.isoformat()
                )

        stage = "port_forward"
        local_port = pick_local_port()
        log(
            f"{run_id}: stage={stage} (starting port-forward on local port {local_port})"
        )
        port_forward = start_port_forward(local_port)

        stage = "health"
        log(f"{run_id}: stage={stage} (waiting for /health)")
        t2_monotonic, port_forward = wait_for_health(local_port, port_forward)
        metadata["timestamps"]["t2_local_monotonic"] = t2_monotonic

        stage = "model_id"
        log(f"{run_id}: stage={stage} (discovering model id)")
        model_id, port_forward = wait_for_model_id(local_port, port_forward)
        metadata["served_model_id"] = model_id
        log(f"{run_id}: discovered model_id={model_id}")

        stage = "request"
        log(f"{run_id}: stage={stage} (sending streaming request)")
        t3_monotonic, t4_monotonic, t5_monotonic = send_streaming_request(
            local_port, model_id
        )
        metadata["timestamps"]["t3_local_monotonic"] = t3_monotonic
        metadata["timestamps"]["t4_local_monotonic"] = t4_monotonic
        metadata["timestamps"]["t5_local_monotonic"] = t5_monotonic
        row["status"] = "ok"
        log(
            f"{run_id}: request complete "
            f"request_to_first_token_s={format_seconds(t4_monotonic - t3_monotonic)} "
            f"request_to_done_s={format_seconds(t5_monotonic - t3_monotonic)}"
        )

    except BenchmarkError as error:
        row["error_stage"] = error.stage
        row["error_message"] = one_line(str(error))
        log(f"{run_id}: failed stage={error.stage} error={row['error_message']}")
    except Exception as error:  # noqa: BLE001
        row["error_stage"] = stage
        row["error_message"] = one_line(f"{type(error).__name__}: {error}")
        log(f"{run_id}: failed stage={stage} error={row['error_message']}")
    finally:
        if port_forward is not None:
            metadata["port_forward_output"] = stop_process(port_forward)

        if t0_monotonic is not None and t2_monotonic is not None:
            row["deploy_to_health_s"] = format_seconds(t2_monotonic - t0_monotonic)
        if t2_monotonic is not None and t3_monotonic is not None:
            row["health_to_first_request_s"] = format_seconds(
                t3_monotonic - t2_monotonic
            )
        if t0_monotonic is not None and t3_monotonic is not None:
            row["deploy_to_first_request_s"] = format_seconds(
                t3_monotonic - t0_monotonic
            )
        if t0_monotonic is not None and t4_monotonic is not None:
            row["deploy_to_first_token_s"] = format_seconds(t4_monotonic - t0_monotonic)
        if t3_monotonic is not None and t4_monotonic is not None:
            row["request_to_first_token_s"] = format_seconds(
                t4_monotonic - t3_monotonic
            )
        if t3_monotonic is not None and t5_monotonic is not None:
            row["request_to_done_s"] = format_seconds(t5_monotonic - t3_monotonic)

        t0_wall_parsed = parse_k8s_time(row.get("t0_utc") or "")
        t1_wall_parsed = parse_k8s_time(row.get("container_started_at_utc") or "")
        if t0_wall_parsed is not None and t1_wall_parsed is not None:
            scale_to_container = (t1_wall_parsed - t0_wall_parsed).total_seconds()
            row["scale_to_container_start_s"] = format_seconds(scale_to_container)
            deploy_to_health = row.get("deploy_to_health_s") or ""
            if deploy_to_health:
                row["container_start_to_health_s"] = format_seconds(
                    float(deploy_to_health) - scale_to_container
                )

        if row["status"] != "ok" and not row["error_stage"]:
            row["error_stage"] = stage

        artifact_summary: dict[str, Any] = {}
        if pod_name:
            artifact_summary = capture_pod_artifacts(
                run_dir,
                pod_name,
                container_started_at_utc=row.get("container_started_at_utc", ""),
            )

        event_insights = artifact_summary.get("event_insights") or {}
        row["vllm_image_ref"] = str(event_insights.get("vllm_image_ref", ""))
        row["vllm_image_pull_state"] = str(
            event_insights.get("vllm_image_pull_state", "")
        )
        row["vllm_image_pull_duration_s"] = str(
            event_insights.get("vllm_image_pull_duration_s", "")
        )
        row["vllm_image_pull_reported_s"] = str(
            event_insights.get("vllm_image_pull_reported_s", "")
        )
        row["init_image_ref"] = str(event_insights.get("init_image_ref", ""))
        row["init_image_pull_state"] = str(
            event_insights.get("init_image_pull_state", "")
        )
        row["init_image_pull_duration_s"] = str(
            event_insights.get("init_image_pull_duration_s", "")
        )
        row["init_image_pull_reported_s"] = str(
            event_insights.get("init_image_pull_reported_s", "")
        )

        log_insights = artifact_summary.get("log_insights") or {}
        container_to_health = row.get("container_start_to_health_s") or ""
        if log_insights and container_to_health:
            try:
                total = float(container_to_health)
                boot = float(log_insights.get("boot_to_model_start_s") or 0)
                model = float(log_insights.get("model_loading_s") or 0)
                compile_total = float(log_insights.get("torch_compile_s") or 0)
                cuda_graph = float(log_insights.get("cuda_graph_capture_s") or 0)
                remaining = total - (boot + model + compile_total + cuda_graph)
                log_insights["remaining_engine_warmup_to_health_s"] = format_seconds(
                    remaining
                )
                artifact_summary["log_insights"] = log_insights
                write_json(run_dir / "log-insights.json", log_insights)
            except ValueError:
                pass

        try:
            scale_deployment(0)
            wait_for_no_pods()
        except BenchmarkError as error:
            if row["status"] == "ok":
                row["status"] = "failed"
                row["error_stage"] = "cleanup"
                row["error_message"] = one_line(str(error))

        metadata["row"] = row
        if artifact_summary:
            metadata["artifact_summary"] = artifact_summary
        write_json(run_dir / "run.json", metadata)
        append_csv_row(row)
        write_json(
            session_run_dir / "summary.json",
            {
                "run_id": run_id,
                "variant": variant,
                "run_kind": run_kind,
                "raw_run_dir": str(run_dir),
                "row": row,
                "artifact_summary": artifact_summary,
            },
        )
        log(f"{run_id}: wrote row to {RESULTS_CSV} and artifacts to {run_dir}")

    return row


def apply_variant_manifests(variant: str) -> None:
    config = VARIANTS[variant]
    run_kubectl(
        [
            "-n",
            NAMESPACE,
            "delete",
            "deployment",
            DEPLOYMENT,
            "--ignore-not-found=true",
        ],
        timeout=180,
    )

    for path in BASELINE_APPLY_FILES:
        apply_manifest_file(path)

    for path in config.extra_apply_files:
        apply_manifest_file(path)

    if config.config_patch_file:
        patch_resource("configmap", "benchmark-config", config.config_patch_file)

    if config.deployment_patch_file:
        patch_path = resolve_repo_file(config.deployment_patch_file)
        if variant == "v4":
            text = patch_path.read_text(encoding="utf-8")
            if "REPLACE_ME/" in text:
                raise BenchmarkError(
                    "variant",
                    "v4 deployment image still contains REPLACE_ME/. Update k8s/variants/runai-streamer/deployment-patch.yaml first.",
                )
        patch_resource("deployment", DEPLOYMENT, config.deployment_patch_file)


def print_row(row: dict[str, str]) -> None:
    if row["status"] == "ok":
        print(
            " ".join(
                [
                    f"[{row['run_kind']}]",
                    f"deploy_to_health_s={row['deploy_to_health_s']}",
                    f"deploy_to_first_token_s={row['deploy_to_first_token_s']}",
                    f"request_to_first_token_s={row['request_to_first_token_s']}",
                    f"request_to_done_s={row['request_to_done_s']}",
                ]
            )
        )
        return
    print(
        " ".join(
            [
                f"[{row['run_kind']}]",
                "status=failed",
                f"error_stage={row['error_stage']}",
                f"error={row['error_message']}",
            ]
        )
    )


def main() -> int:
    args = parse_args()
    variant = args.variant
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    session_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{variant}"
    session_dir = RESULTS_DIR / "tmp" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    set_session_log(session_dir / "session.log")
    log(f"Session temp dir: {session_dir}")

    init_kube_ops()

    log(f"Applying manifests for {variant}...")
    apply_variant_manifests(variant)
    image_streaming_state = validate_variant_image_streaming(variant)
    log(f"Observed node image streaming state: {image_streaming_state}")

    log(f"Running {variant} cold iteration...")
    cold = run_single_iteration(
        variant=variant,
        run_kind="cold",
        clear_cache=True,
        image_streaming_state=image_streaming_state,
        session_dir=session_dir,
    )
    print_row(cold)

    log(f"Running {variant} warm iteration...")
    warm = run_single_iteration(
        variant=variant,
        run_kind="warm",
        clear_cache=False,
        image_streaming_state=image_streaming_state,
        session_dir=session_dir,
    )
    print_row(warm)

    failures = [row for row in (cold, warm) if row["status"] != "ok"]
    if failures:
        print(f"Completed with {len(failures)} failed iteration(s).", file=sys.stderr)
        set_session_log(None)
        return 1

    log("Completed successfully.")
    set_session_log(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
