# Run:ai Streamer Image Build

Build the image used by variants `v4` and `v5` (`--load-format runai_streamer`).

## Purpose

`vllm/vllm-openai:v0.18.0` does not include Run:ai extras by default.
This Dockerfile installs `vllm[runai]==0.18.0` at build time.

## Build And Push

```bash
export RUNAI_IMAGE=us-docker.pkg.dev/<project>/<repo>/vllm-openai-runai:v0.18.0
docker build -t "$RUNAI_IMAGE" -f docker/runai-streamer/Dockerfile .
docker push "$RUNAI_IMAGE"
```

## Use in v4 and v5

Set that image in `k8s/variants/runai-streamer/deployment-patch.yaml` on line 11, then run:

```bash
uv run python scripts/record_cold_start_run.py --variant v4
uv run python scripts/record_cold_start_run.py --variant v5
```
