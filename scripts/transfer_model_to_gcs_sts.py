import argparse
import fnmatch
import json
import os
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MODEL_ID = "mistralai/Mistral-7B-Instruct-v0.3"
DEFAULT_REVISION = "c170c708c41dac9275d15a8fff4eca08d52bab71"
DEFAULT_PROJECT_ID = "llm-benchmark-startup"
DEFAULT_BUCKET = "llm-models-benchmark"

DEFAULT_INCLUDE_PATTERNS = [
    "config.json",
    "generation_config.json",
    "model.safetensors.index.json",
    "model-*.safetensors",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer.model.v3",
    "tokenizer_config.json",
    "params.json",
    "*.tiktoken",
]

DEFAULT_EXCLUDE_PATTERNS = [
    "consolidated.safetensors",
]


class TransferError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Transfer Hugging Face model artifacts to GCS using Storage Transfer Service "
            "URL list flow (no local model byte relay)."
        )
    )
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument(
        "--dest-prefix",
        default=None,
        help="Destination GCS prefix. Defaults to model-id.",
    )
    parser.add_argument(
        "--url-list-object",
        default=None,
        help="GCS object path for URL list TSV under the bucket.",
    )
    parser.add_argument(
        "--stage-prefix",
        default=None,
        help="Temporary staging prefix used by Storage Transfer Service.",
    )
    parser.add_argument(
        "--allow-pattern",
        action="append",
        default=[],
        help="Extra include glob for model files.",
    )
    parser.add_argument(
        "--exclude-pattern",
        action="append",
        default=[],
        help="Extra exclude glob for model files.",
    )
    parser.add_argument(
        "--hf-token-env",
        default="HF_TOKEN",
        help="Optional HF token environment variable for private models.",
    )
    parser.add_argument(
        "--operation-timeout",
        type=int,
        default=7200,
        help="Timeout in seconds for transfer operation completion.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Polling interval in seconds while waiting for operation completion.",
    )
    parser.add_argument(
        "--keep-job",
        action="store_true",
        help="Keep transfer job after completion instead of deleting it.",
    )
    parser.add_argument(
        "--keep-staging",
        action="store_true",
        help="Keep staging objects under stage prefix after flattening.",
    )
    parser.add_argument(
        "--output",
        default="results/sts_transfer_last.json",
        help="Write run metadata JSON to this path.",
    )
    return parser.parse_args()


def run(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    print(f"$ {shlex.join(command)}", flush=True)
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=capture_output,
    )


def run_checked(
    command: list[str], *, capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    completed = run(command, capture_output=capture_output)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        message = stderr or stdout or f"command failed with code {completed.returncode}"
        raise TransferError(f"{' '.join(command)} failed: {message}")
    return completed


def gcloud_token() -> str:
    completed = run_checked(["gcloud", "auth", "print-access-token"], capture_output=True)
    token = (completed.stdout or "").strip()
    if not token:
        raise TransferError("Unable to obtain gcloud access token")
    return token


def api_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    quota_project: str | None = None,
) -> dict[str, Any]:
    token = gcloud_token()
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(url=url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if quota_project:
        request.add_header("x-goog-user-project", quota_project)
    if payload is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise TransferError(f"{method} {url} failed: {error.code} {detail}") from error
    except urllib.error.URLError as error:
        raise TransferError(f"{method} {url} failed: {error}") from error

    if not raw.strip():
        return {}
    return json.loads(raw)


def fetch_hf_tree(model_id: str, revision: str, token: str | None) -> list[dict[str, Any]]:
    model_component = urllib.parse.quote(model_id, safe="/")
    url = f"https://huggingface.co/api/models/{model_component}/tree/{revision}?recursive=1"
    request = urllib.request.Request(url=url, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def select_model_files(
    tree: list[dict[str, Any]],
    include_patterns: list[str],
    exclude_patterns: list[str],
    *,
    model_id: str,
    revision: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in tree:
        if item.get("type") != "file":
            continue
        path = str(item.get("path", ""))
        if not path:
            continue
        if any(fnmatch.fnmatch(path, pattern) for pattern in exclude_patterns):
            continue
        if not any(fnmatch.fnmatch(path, pattern) for pattern in include_patterns):
            continue

        quoted_path = urllib.parse.quote(path, safe="/")
        url = f"https://huggingface.co/{model_id}/resolve/{revision}/{quoted_path}"
        size = item.get("size") if isinstance(item.get("size"), int) else None
        rows.append({"path": path, "url": url, "size": size})

    rows.sort(key=lambda row: str(row["url"]))
    if not rows:
        raise TransferError("No model files selected from Hugging Face tree")
    return rows


def write_url_list_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = ["TsvHttpData-1.0"]
    for row in rows:
        url = str(row["url"])
        size = row.get("size")
        if isinstance(size, int):
            lines.append(f"{url}\t{size}")
        else:
            lines.append(url)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def wait_for_operation(
    name: str,
    timeout: int,
    poll_interval: int,
    quota_project: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    encoded = urllib.parse.quote(name, safe="/")
    url = f"https://storagetransfer.googleapis.com/v1/{encoded}"

    while time.monotonic() < deadline:
        payload = api_json("GET", url, quota_project=quota_project)
        if payload.get("done"):
            return payload

        metadata = payload.get("metadata") or {}
        status = metadata.get("status") or "in_progress"
        print(f"Waiting on transfer operation {name}: status={status}", flush=True)
        time.sleep(max(1, poll_interval))

    raise TransferError(f"Timed out waiting for transfer operation {name}")


def ensure_gcloud() -> None:
    result = run(["gcloud", "--version"], capture_output=True)
    if result.returncode != 0:
        raise TransferError("gcloud CLI is required in PATH")


def ensure_required_paths_exist(bucket: str, dest_prefix: str, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        path = str(row["path"])
        uri = f"gs://{bucket}/{dest_prefix.strip('/')}/{path}"
        run_checked(["gcloud", "storage", "ls", uri], capture_output=True)


def main() -> int:
    args = parse_args()
    ensure_gcloud()

    dest_prefix = (args.dest_prefix or args.model_id).strip("/")
    url_list_object = args.url_list_object or (
        f"_sts/url-lists/{args.model_id.replace('/', '__')}-{args.revision}.tsv"
    )
    stage_prefix = args.stage_prefix or f"_sts/staging/{args.model_id}/{args.revision}"

    include_patterns = sorted(set(DEFAULT_INCLUDE_PATTERNS + list(args.allow_pattern)))
    exclude_patterns = sorted(set(DEFAULT_EXCLUDE_PATTERNS + list(args.exclude_pattern)))
    hf_token = os.getenv(args.hf_token_env) or None

    started_at = datetime.now(UTC)
    summary: dict[str, Any] = {
        "started_at": started_at.isoformat(),
        "project_id": args.project_id,
        "bucket": args.bucket,
        "model_id": args.model_id,
        "revision": args.revision,
        "dest_prefix": dest_prefix,
        "url_list_object": url_list_object,
        "stage_prefix": stage_prefix,
        "include_patterns": include_patterns,
        "exclude_patterns": exclude_patterns,
    }

    with tempfile.TemporaryDirectory(prefix="sts-hf-url-list-") as tmp:
        tsv_path = Path(tmp) / "hf-url-list.tsv"

        print("Fetching Hugging Face model tree...", flush=True)
        tree = fetch_hf_tree(args.model_id, args.revision, hf_token)

        rows = select_model_files(
            tree,
            include_patterns,
            exclude_patterns,
            model_id=args.model_id,
            revision=args.revision,
        )
        summary["selected_files"] = [{"path": row["path"], "size": row["size"]} for row in rows]

        write_url_list_tsv(tsv_path, rows)

        print("Ensuring Storage Transfer API is enabled...", flush=True)
        run_checked([
            "gcloud",
            "services",
            "enable",
            "storagetransfer.googleapis.com",
            "--project",
            args.project_id,
        ])

        print("Ensuring Cloud Resource Manager API is enabled...", flush=True)
        run_checked([
            "gcloud",
            "services",
            "enable",
            "cloudresourcemanager.googleapis.com",
            "--project",
            args.project_id,
        ])

        print("Ensuring Storage Transfer IAM prerequisites...", flush=True)
        run_checked([
            "gcloud",
            "transfer",
            "authorize",
            "--add-missing",
            "--project",
            args.project_id,
            "--quiet",
        ])

        print("Uploading URL list TSV to GCS...", flush=True)
        url_list_uri = f"gs://{args.bucket}/{url_list_object}"
        run_checked(["gcloud", "storage", "cp", str(tsv_path), url_list_uri])

        print("Resolving Storage Transfer service agent...", flush=True)
        service_account_response = api_json(
            "GET",
            f"https://storagetransfer.googleapis.com/v1/googleServiceAccounts/{args.project_id}",
            quota_project=args.project_id,
        )
        service_account = str(service_account_response.get("accountEmail", "")).strip()
        if not service_account:
            raise TransferError("Could not resolve Storage Transfer service account")
        summary["storage_transfer_service_account"] = service_account

        print(
            "Granting bucket IAM roles for Storage Transfer service agent...",
            flush=True,
        )
        bucket_uri = f"gs://{args.bucket}"
        run_checked([
            "gcloud",
            "storage",
            "buckets",
            "add-iam-policy-binding",
            bucket_uri,
            "--member",
            f"serviceAccount:{service_account}",
            "--role",
            "roles/storage.objectAdmin",
        ])
        run_checked([
            "gcloud",
            "storage",
            "buckets",
            "add-iam-policy-binding",
            bucket_uri,
            "--member",
            f"serviceAccount:{service_account}",
            "--role",
            "roles/storage.legacyBucketReader",
        ])

        print("Creating Storage Transfer job...", flush=True)
        create_payload = {
            "description": (f"hf-to-gcs-{args.model_id.replace('/', '__')}-{args.revision[:12]}"),
            "projectId": args.project_id,
            "status": "ENABLED",
            "transferSpec": {
                "httpDataSource": {"listUrl": url_list_uri},
                "gcsDataSink": {
                    "bucketName": args.bucket,
                    "path": f"{stage_prefix.strip('/')}/",
                },
            },
        }
        create_response = api_json(
            "POST",
            "https://storagetransfer.googleapis.com/v1/transferJobs",
            payload=create_payload,
            quota_project=args.project_id,
        )
        job_name = str(create_response.get("name", "")).strip()
        if not job_name:
            raise TransferError(f"Transfer job creation failed: {create_response}")
        summary["job_name"] = job_name

        print(f"Running transfer job {job_name}...", flush=True)
        run_response = api_json(
            "POST",
            f"https://storagetransfer.googleapis.com/v1/{urllib.parse.quote(job_name, safe='/')}:run",
            payload={"projectId": args.project_id},
            quota_project=args.project_id,
        )
        operation_name = str(run_response.get("name", "")).strip()
        if not operation_name:
            raise TransferError(f"Transfer run start failed: {run_response}")
        summary["operation_name"] = operation_name

        print(f"Waiting for operation completion: {operation_name}", flush=True)
        operation_payload = wait_for_operation(
            operation_name,
            timeout=args.operation_timeout,
            poll_interval=args.poll_interval,
            quota_project=args.project_id,
        )
        summary["operation_result"] = operation_payload
        if operation_payload.get("error"):
            raise TransferError(f"Transfer operation failed: {operation_payload['error']}")

        print("Flattening staging layout into benchmark destination prefix...", flush=True)
        source_prefix = (
            f"gs://{args.bucket}/{stage_prefix.strip('/')}/huggingface.co/"
            f"{args.model_id}/resolve/{args.revision}"
        )
        destination_prefix_uri = f"gs://{args.bucket}/{dest_prefix}"
        run_checked([
            "gcloud",
            "storage",
            "rsync",
            "--recursive",
            source_prefix,
            destination_prefix_uri,
        ])

        print("Verifying mirrored files in final destination prefix...", flush=True)
        ensure_required_paths_exist(args.bucket, dest_prefix, rows)

        if not args.keep_staging:
            print("Removing staging objects...", flush=True)
            run_checked([
                "gcloud",
                "storage",
                "rm",
                "--recursive",
                f"gs://{args.bucket}/{stage_prefix.strip('/')}",
            ])
            summary["staging_removed"] = True
        else:
            summary["staging_removed"] = False

        if not args.keep_job:
            print("Deleting transfer job...", flush=True)
            run_checked(["gcloud", "transfer", "jobs", "delete", job_name, "--quiet"])
            summary["job_deleted"] = True
        else:
            summary["job_deleted"] = False

    finished_at = datetime.now(UTC)
    summary["finished_at"] = finished_at.isoformat()
    summary["duration_seconds"] = (finished_at - started_at).total_seconds()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote transfer summary to {output_path}")
    print("Storage Transfer model mirror complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
