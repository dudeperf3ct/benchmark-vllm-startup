# Prerequisites and Permissions

This document lists the required setup for running the benchmark flow.

## 1) Local tooling

Required CLIs:

- `gcloud`
- `kubectl`
- `tofu`
- `python3`
- `uv`

Quick check:

```bash
gcloud --version
kubectl version --client
tofu version
python3 --version
uv --version
```

## 2) Active project/account

```bash
export PROJECT_ID="llm-benchmark-startup"
gcloud config set project "$PROJECT_ID"
gcloud auth login
```

## 3) Required Google APIs

These are required for infra + benchmark + model transfer workflows:

- `compute.googleapis.com`
- `container.googleapis.com`
- `containerfilesystem.googleapis.com`
- `storage.googleapis.com`
- `storagetransfer.googleapis.com`
- `cloudresourcemanager.googleapis.com`

Enable explicitly (safe to rerun):

```bash
gcloud services enable \
  compute.googleapis.com \
  container.googleapis.com \
  containerfilesystem.googleapis.com \
  storage.googleapis.com \
  storagetransfer.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project "$PROJECT_ID"
```

Note: `infra/gcp/main.tf` enables these APIs during `tofu apply` as well.

## 4) IAM permissions (human operator)

### Recommended (fastest)

Use a project Owner account for benchmark setup and execution.

### If not using Owner

Grant equivalent privileges required by this repo's workflows:

- infra provisioning: GKE + Compute + VPC + Service Usage
- bucket and object operations
- Storage Transfer job management and IAM policy updates
- Artifact Registry operations for V4 image push

Practical role set (single user):

- `roles/container.admin`
- `roles/compute.admin`
- `roles/storage.admin`
- `roles/storagetransfer.admin`
- `roles/serviceusage.serviceUsageAdmin`
- `roles/resourcemanager.projectIamAdmin`
- `roles/iam.serviceAccountUser`
- `roles/artifactregistry.admin` (or `roles/artifactregistry.repoAdmin` + writer roles)

Grant example:

```bash
export USER_EMAIL="you@example.com"

for ROLE in \
  roles/container.admin \
  roles/compute.admin \
  roles/storage.admin \
  roles/storagetransfer.admin \
  roles/serviceusage.serviceUsageAdmin \
  roles/resourcemanager.projectIamAdmin \
  roles/iam.serviceAccountUser \
  roles/artifactregistry.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="user:${USER_EMAIL}" \
    --role="$ROLE"
done
```

## 5) Storage Transfer prerequisites

The STS helper script uses:

```bash
uv run python scripts/transfer_model_to_gcs_sts.py
```

It automatically runs:

- `gcloud transfer authorize --add-missing`

This grants required roles to:

- your user (transfer admin/agent permissions)
- Google-managed service account `project-<NUMBER>@storage-transfer-service.iam.gserviceaccount.com`

If you need to inspect that service account manually:

```bash
curl -s \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: ${PROJECT_ID}" \
  "https://storagetransfer.googleapis.com/v1/googleServiceAccounts/${PROJECT_ID}"
```

## 6) Runtime access to model bucket from workloads

V2/V3/V4 pods must read from GCS.

In this repo's default setup, GPU nodes use cloud-platform scope and workload identity is enabled.

`infra/gcp/main.tf` now applies required bucket IAM bindings automatically for:

- node compute service account (`<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`)
- workload identity principal (`ns/llm-bench/sa/default`)

So after `tofu apply`, manual IAM grants are usually not required.

Manual fallback (if IAM drift exists):

Bucket read grant example:

```bash
export BUCKET="llm-models-benchmark"
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export NODE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
export WI_PRINCIPAL="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${PROJECT_ID}.svc.id.goog/subject/ns/llm-bench/sa/default"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${NODE_SA}" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${NODE_SA}" \
  --role="roles/storage.legacyBucketReader"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="${WI_PRINCIPAL}" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="${WI_PRINCIPAL}" \
  --role="roles/storage.legacyBucketReader"
```

## 7) V4 image push prerequisites

V4 needs a custom image with `vllm[runai]`.

Create registry repo (once):

```bash
export REGION="europe-west1"
gcloud artifacts repositories create llm-bench \
  --repository-format=docker \
  --location="$REGION" \
  --description="Benchmark images"
```

Configure Docker auth:

```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"
```

Build and push:

```bash
export RUNAI_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/llm-bench/vllm-openai-runai:v0.18.0"
docker build -t "$RUNAI_IMAGE" -f docker/runai-streamer/Dockerfile .
docker push "$RUNAI_IMAGE"
```

Then replace image placeholder in:

- `k8s/variants/runai-streamer/deployment-patch.yaml`

## 8) End-to-end command order

1. Provision infra (image streaming off for V1/V2):

```bash
tofu -chdir=infra/gcp apply -auto-approve
gcloud container clusters get-credentials benchmark-cold-start --zone europe-west1-c --project "$PROJECT_ID"
```

2. Transfer model to GCS:

```bash
uv run python scripts/transfer_model_to_gcs_sts.py
```

3. Run the non-streaming block (`v0`, `v1`, `v2`, optional `v5`):

```bash
uv run python scripts/record_cold_start_run.py --variant v0
uv run python scripts/record_cold_start_run.py --variant v1
uv run python scripts/record_cold_start_run.py --variant v2
uv run python scripts/record_cold_start_run.py --variant v5
```

4. Enable image streaming and re-apply infra:

```bash
# set gpu_enable_image_streaming=true in infra/gcp/terraform.tfvars
tofu -chdir=infra/gcp apply -auto-approve
```

5. Run the streaming-enabled block (`v3`, `v4`):

```bash
uv run python scripts/record_cold_start_run.py --variant v3
uv run python scripts/record_cold_start_run.py --variant v4
```

## 9) Common blockers

- `ZONE_RESOURCE_POOL_EXHAUSTED`: no GPU stock in selected zone; switch zone and retry.
- STS API quota-project/permission errors: ensure APIs enabled and rerun `gcloud transfer authorize --add-missing`.
- V4 fail-fast `REPLACE_ME/...`: update image URI in `k8s/variants/runai-streamer/deployment-patch.yaml`.
