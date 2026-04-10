# Run:ai Streamer Variants (V4, V5)

Variant patch set for direct GCS model loading via `runai_streamer`.

## Changes From Baseline

- uses a custom image with `vllm[runai]`
- sets `--load-format runai_streamer`
- points model id to `gs://...`

## Files

- `benchmark-config-patch.yaml`
- `deployment-patch.yaml`

## Prerequisites

1. Mirror artifacts to GCS.

```bash
uv run python scripts/transfer_model_to_gcs_sts.py
```

2. Build and push the Run:ai image from `docker/runai-streamer/`.
3. Update the image in `deployment-patch.yaml` for your registry.
4. Ensure workload identity or service account can read model bucket metadata and objects.
5. Enable image streaming for `v4`; keep it disabled for `v5`.

## Run

```bash
uv run python scripts/record_cold_start_run.py --variant v4
uv run python scripts/record_cold_start_run.py --variant v5
```
