# Benchmark Cold Start

In this project, we will benchmark the vLLM startup time on GKE under different settings. The blog provides visualization charts for the results and insights for each of the variants.

Blog: https://dudeperf3ct.github.io/posts/vllm_startup_load_time/

The following definition is used for 6 variants used in the experiment.

- **V0**: vllm-image-cold reference, image streaming **off**
- **V1**: baseline PVC HF-backed model path same serving config as V0, image streaming **off**
- **V2**: GCS -> sync to PVC -> normal loader, image streaming **off**
- **V3**: same manifests as V2, image streaming **on**
- **V4**: `runai_streamer` from `gs://...`, image streaming **on**
- **V5**: `runai_streamer` from `gs://...`, image streaming **off**

## Repository Layout

- `infra/gcp/` infrastructure (OpenTofu, GKE, GCS)
- `k8s/baseline/` baseline manifests
- `k8s/variants/` variant patches
- `scripts/` benchmark and model-transfer scripts
- `docker/runai-streamer/` Run:ai image build files

## Start Here

The first step would be to provision the infrastructure. I use [`mise`](https://github.com/jdx/mise) to configure the project and tools required.

```bash
mise install
```

> [!TIP]
> More information on provisioning the infrastructure can be found at [infra](infra/gcp/README.md) readme.

Once the required dev tools are setup, we are all set to provision the infrastructure. This requires a GCP account and a project to get started. The [`terraform.tfvars.example`](infra/gcp/terraform.tfvars.example) file provides the input variables that needs to be configured to provision the infrastructure.

> [!TIP]
> More information on the required permissions can be found at [PREREQUISITES_AND_PERMISSIONS.md](docs/PREREQUISITES_AND_PERMISSIONS.md) document.

The [end to end commands](docs/PREREQUISITES_AND_PERMISSIONS.md#8-end-to-end-command-order) section provides all the commands required to reproduce the experiments.

## Capture Transport Baseline

Run this before the main benchmark matrix:

```bash
uv run python scripts/capture_benchmark_context.py \
  --infra-provider gcp \
  --cluster-region <region> \
  --artifact-url 'https://huggingface.co/mistralai/Mistral-7B-Instruct-v0.3/resolve/main/model-00001-of-00003.safetensors'
```

This writes `results/context.json` with DNS, connect, TTFB, and sampled download metrics.

If you also need runtime image pull bandwidth, run one uncached cold start on a fresh node and inspect the `Pulled` event in `results/raw/<run_id>/events.json`.

## Run One Variant

Once infrastructure is up, we are all set to run our benchmarking experiments. Before running the experiment, it is recommended to collect the environment information in the previous [Transport](<README#Changes From Baseline>) section.

> [!TIP]
> The variant specific readmes can be found under [`k8s`](./k8s) folder.

```bash
uv run python scripts/record_cold_start_run.py --variant v0
```

Active variants:

- `v0` image-cold reference (same serving config as `v1`, run first on a fresh node)
- `v1` baseline HF path, image streaming off
- `v2` object-storage sync-to-PVC path, image streaming off
- `v3` same as `v2`, image streaming on
- `v4` Run:ai streamer path, image streaming on
- `v5` Run:ai streamer path, image streaming off

Each execution runs one `cold` pass and one `warm` pass, then appends rows to `results/results.csv`.

> [!NOTE]
> Before running `v4` and `v5` variants, a new docker image is built and used on top of base vllm image. To know get started, refer to the [`runai-streamer`](docker/runai-streamer/README.md) readme.


## Cold / Warm Definitions and Setup

- Cold run means: `scale from 0 to 1 after clearing relevant caches where practical; if full cache clearing is not possible, label runs as node-cold and document exactly what was reset`
- Warm run means: `scale from 0 to 1 on the same node shortly after a prior successful run, with image/model caches likely still present`

Runner policy used in this repo:

- Warm runs do **not** clear PVCs.
- Cold cache reset is variant-specific:
  - `v0`, `v1`, `v4`, `v5`: clear `vllm-hf-cache` only
  - `v2`, `v3`: clear both `vllm-hf-cache` and `vllm-model-store`

Variant behavior:

| Variant | Cold cache reset | Warm cache reset | Model source at startup | Init sync behavior |
|---|---|---|---|---|
| `v0` | clear `vllm-hf-cache` | none | HF (`mistralai/...`) | none |
| `v1` | clear `vllm-hf-cache` | none | HF (`mistralai/...`) | none |
| `v2` | clear `vllm-hf-cache`, `vllm-model-store` | none | `/model-store` PVC | syncs from GCS only when PVC is not populated |
| `v3` | clear `vllm-hf-cache`, `vllm-model-store` | none | `/model-store` PVC | syncs from GCS only when PVC is not populated |
| `v4` | clear `vllm-hf-cache` | none | `gs://...` via Run:ai streamer | none |
| `v5` | clear `vllm-hf-cache` | none | `gs://...` via Run:ai streamer | none |

## Transfer Model Artifacts To GCS

Once infrastructure is up, in a separate terminal run the following command to transfer the LLM weights to GCS bucket. These are required for running `v2` variant of the experiment.

```bash
uv run python scripts/transfer_model_to_gcs_sts.py
```

> [!TIP]
> More information on the STS script can be found at [STS_MODEL_TRANSFER.md](docs/STS_MODEL_TRANSFER.md) document.

## Result Artifacts

Running the benchmarks creates a `results` folder with the following structure.

- `results/results.csv` KPI rows
- `results/results.csv` also records per-container image pull fields (`vllm_image_*`, `init_image_*`)
- `results/results.csv` includes lifecycle timing fields for charting and attribution
- `results/raw/<run_id>/` pod/events/log snapshots and run metadata
- `results/tmp/<session_id>/` session logs and per-run summaries

## Future Explorations

- [ ] Benchmark model loading from different disk types. Presently, we are using `pd-balanced`. Alternative speed up could be observed from `pd-ssd`.
- [ ] [Nydus](https://github.com/containerd/nydus-snapshotter) or [eStargz](https://github.com/containerd/stargz-snapshotter) snapshotter variant of images to minimize first cold start vLLM container startup