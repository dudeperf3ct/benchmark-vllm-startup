# GCP Infrastructure

Minimal GKE infrastructure for the benchmark workflow.

## Resources

- VPC and subnetwork
- GKE cluster and one GPU node pool
- GCS bucket for model artifacts
- bucket IAM bindings for benchmark runtime identities (compute service account + `llm-bench/default` workload identity)
- required project APIs
- optional image streaming on GPU nodes (`gpu_enable_image_streaming`)

## Prerequisite Doc

- `docs/PREREQUISITES_AND_PERMISSIONS.md`

## Quick Start

From repo root:

```bash
cp infra/gcp/terraform.tfvars.example infra/gcp/terraform.tfvars
# edit infra/gcp/terraform.tfvars

tofu -chdir=infra/gcp init
tofu -chdir=infra/gcp plan
tofu -chdir=infra/gcp apply
```

Configure `kubectl`:

```bash
tofu -chdir=infra/gcp output -raw gke_get_credentials_command
```

Run the printed `gcloud container clusters get-credentials ...` command.

## Suggested Run Sequence

1. Set `gpu_enable_image_streaming=false`, apply, then run `v0`, `v1`, `v2`
2. Set `gpu_enable_image_streaming=true`, apply, then run `v3` and `v4`

Changing `gpu_enable_image_streaming` updates node configuration and may recreate nodes.

## Teardown

```bash
tofu -chdir=infra/gcp destroy -auto-approve
```

If bucket deletion fails because objects still exist, clear bucket contents and run destroy again.
