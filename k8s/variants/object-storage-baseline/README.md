# Object Storage Variants (V2, V3)

Variant patch set for object-storage loading while keeping the serving path close to baseline.

## Changes From Baseline

- adds `vllm-model-store` PVC
- init container syncs model files from GCS to `/model-store` only when the PVC is not already populated
- vLLM loads from local `/model-store`

The same manifests are used for both variants:

- `v2` image streaming disabled
- `v3` image streaming enabled

## Files

- `benchmark-config-patch.yaml`
- `pvc.yaml`
- `deployment-patch.yaml`

## Prerequisites

1. Mirror model artifacts to GCS.

```bash
uv run python scripts/transfer_model_to_gcs_sts.py
```

2. Ensure workload identity or service account can read the model bucket.

## Run

```bash
uv run python scripts/record_cold_start_run.py --variant v2
uv run python scripts/record_cold_start_run.py --variant v3
```
