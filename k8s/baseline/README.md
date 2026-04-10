# Baseline Manifests

Baseline manifest set for the benchmark deployment (`v0`) and (`v1`).

Here, 

- `v0` acts as a very first run on a fresh node, benchmarking both cold and warm runs.
- `v1` repeats the same experiment for cold and warm runs for the second time.

## Files

- `namespace.yaml`
- `benchmark-config.yaml`
- `pvc.yaml`
- `service.yaml`
- `deployment.yaml`

## Key Properties

- namespace: `llm-bench`
- deployment: `vllm-baseline`
- initial replicas: `0`
- cache PVC: `vllm-hf-cache`

`replicas: 0` is intentional so each benchmark iteration controls a `0 -> 1` startup.

## Recommended Usage

Use the benchmark runner instead of applying these files manually:

```bash
uv run python scripts/record_cold_start_run.py --variant v0
uv run python scripts/record_cold_start_run.py --variant v1
```
