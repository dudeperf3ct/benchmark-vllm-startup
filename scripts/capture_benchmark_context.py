import argparse
import json
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture benchmark environment metadata."
    )
    parser.add_argument(
        "--output", default="results/context.json", help="JSON output path"
    )
    parser.add_argument("--infra-provider", default="gcp")
    parser.add_argument("--cluster-region", default="unknown")
    parser.add_argument("--namespace", default="llm-bench")
    parser.add_argument(
        "--artifact-url",
        action="append",
        default=[],
        help="Artifact URL to probe from inside the cluster; pass a real model shard/object URL, not just a model API page",
    )
    parser.add_argument(
        "--sample-bytes",
        type=int,
        default=67_108_864,
        help="Sample size for ranged artifact probe; defaults to 64 MiB for a more representative throughput measurement",
    )
    parser.add_argument(
        "--full-download",
        action="store_true",
        help="Download the full artifact instead of using a ranged probe",
    )
    parser.add_argument("--probe-timeout", type=int, default=120)
    parser.add_argument("--pod-timeout", type=int, default=300)
    parser.add_argument("--probe-image", default="curlimages/curl:8.12.1")
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def run_command(command: list[str]) -> dict[str, object]:
    result = subprocess.run(command, capture_output=True, text=True)
    return {
        "command": command,
        "command_pretty": shlex.join(command),
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def maybe_run(command: list[str]) -> dict[str, object] | None:
    if shutil.which(command[0]) is None:
        return None
    return run_command(command)


def run_kubectl(
    args: list[str], *, capture_json: bool = False, stdin: str | None = None
) -> Any:
    result = subprocess.run(
        ["kubectl", *args],
        check=True,
        capture_output=True,
        text=True,
        input=stdin,
    )
    if capture_json:
        return json.loads(result.stdout)
    return result.stdout.strip()


def wait_for_pod_phase(namespace: str, pod_name: str, timeout: int) -> dict[str, Any]:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        pod = run_kubectl(
            ["-n", namespace, "get", "pod", pod_name, "-o", "json"], capture_json=True
        )
        phase = pod.get("status", {}).get("phase")
        if phase in {"Succeeded", "Failed"}:
            return pod
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for pod {pod_name!r} to finish")


def probe_artifact_from_cluster(
    args: argparse.Namespace, url: str, index: int
) -> dict[str, object]:
    pod_name = f"artifact-probe-{int(time.time())}-{index}"
    curl_format = json.dumps(
        {
            "url_effective": "%{url_effective}",
            "http_code": "%{http_code}",
            "time_namelookup": "%{time_namelookup}",
            "time_connect": "%{time_connect}",
            "time_appconnect": "%{time_appconnect}",
            "time_starttransfer": "%{time_starttransfer}",
            "time_total": "%{time_total}",
            "size_download": "%{size_download}",
            "speed_download": "%{speed_download}",
            "remote_ip": "%{remote_ip}",
            "remote_port": "%{remote_port}",
        }
    )
    shell_parts = [
        "curl -L -sS -o /dev/null",
        f"--max-time {args.probe_timeout}",
    ]
    if not args.full_download:
        range_end = args.sample_bytes - 1
        shell_parts.append(f"--range 0-{range_end}")
    shell_parts.extend([f"--write-out '{curl_format}'", '"$PROBE_URL"'])
    shell_command = " ".join(shell_parts)
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": args.namespace},
        "spec": {
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "probe",
                    "image": args.probe_image,
                    "command": ["sh", "-lc", shell_command],
                    "env": [{"name": "PROBE_URL", "value": url}],
                }
            ],
        },
    }

    try:
        run_kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest))
        pod = wait_for_pod_phase(args.namespace, pod_name, timeout=args.pod_timeout)
        logs = run_kubectl(["-n", args.namespace, "logs", pod_name])
        node_name = pod.get("spec", {}).get("nodeName")
        phase = pod.get("status", {}).get("phase")
        if phase != "Succeeded":
            return {
                "url": url,
                "ok": False,
                "phase": phase,
                "node_name": node_name,
                "logs": logs,
            }
        return {
            "url": url,
            "ok": True,
            "node_name": node_name,
            "mode": "full_download" if args.full_download else "range_probe",
            "sample_bytes": None if args.full_download else args.sample_bytes,
            "metrics": json.loads(logs),
        }
    finally:
        subprocess.run(
            [
                "kubectl",
                "-n",
                args.namespace,
                "delete",
                "pod",
                pod_name,
                "--ignore-not-found=true",
                "--wait=true",
            ],
            check=False,
            capture_output=True,
            text=True,
        )


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "infra_provider": args.infra_provider,
        "cluster_region": args.cluster_region,
        "notes": args.notes,
        "runner": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version,
        },
        "commands": {
            "kubectl_cluster_info": maybe_run(["kubectl", "cluster-info"]),
        },
        "cluster_artifact_probes": [
            probe_artifact_from_cluster(args, url, index)
            for index, url in enumerate(args.artifact_url, start=1)
        ],
    }


def main() -> None:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(args)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    failed = [
        probe for probe in payload["cluster_artifact_probes"] if not probe.get("ok")
    ]
    if failed:
        raise RuntimeError(f"Artifact probe(s) failed: {failed}")
    print(f"Wrote benchmark context to {output}")


if __name__ == "__main__":
    main()
