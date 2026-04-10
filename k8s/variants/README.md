# Variant Manifests

Patch manifests layered on top of `k8s/baseline/`.

## Variants

- `object-storage-baseline/` for `v2` and `v3`
  - sync model files from GCS to `vllm-model-store` when the PVC is empty
  - standard vLLM loading from local PVC
- `runai-streamer/` for `v4` and `v5`
  - load directly from `gs://...`
  - use `--load-format runai_streamer`

## Run Commands

```bash
uv run python scripts/record_cold_start_run.py --variant v2
uv run python scripts/record_cold_start_run.py --variant v3
uv run python scripts/record_cold_start_run.py --variant v4
uv run python scripts/record_cold_start_run.py --variant v5
```

## Related Docs

- `k8s/variants/object-storage-baseline/README.md`
- `k8s/variants/runai-streamer/README.md`
- `docs/STS_MODEL_TRANSFER.md`
