# Model Transfer to GCS via Storage Transfer Service

Use this workflow to mirror model artifacts from Hugging Face to GCS without routing model bytes through your local machine.

This is the recommended path for V2, V3, and V4.

Required permissions and API setup are documented in:

- `docs/PREREQUISITES_AND_PERMISSIONS.md`

## Why STS approach?

- avoids local download/upload bottlenecks
- keeps transfer in Google-managed data paths
- works with the benchmark's fixed model revision

## Transfer Script

```bash
uv run python scripts/transfer_model_to_gcs_sts.py
```

Default values in the helper script are already aligned with this repo:

- project: `llm-benchmark-startup`
- bucket: `llm-models-benchmark`
- model: `mistralai/Mistral-7B-Instruct-v0.3`
- revision: `c170c708c41dac9275d15a8fff4eca08d52bab71`

The helper writes a transfer summary to:

- `results/sts_transfer_last.json`

The helper also runs transfer authorization (`gcloud transfer authorize --add-missing`) to prepare Storage Transfer IAM prerequisites.

## Verify required files exist in final prefix

```bash
gcloud storage ls "gs://${BUCKET}/${DEST_PREFIX}/config.json"
gcloud storage ls "gs://${BUCKET}/${DEST_PREFIX}/model.safetensors.index.json"
gcloud storage ls "gs://${BUCKET}/${DEST_PREFIX}/model-00001-of-00003.safetensors"
gcloud storage ls "gs://${BUCKET}/${DEST_PREFIX}/model-00002-of-00003.safetensors"
gcloud storage ls "gs://${BUCKET}/${DEST_PREFIX}/model-00003-of-00003.safetensors"
```

## Optional cleanup

```bash
gcloud storage rm --recursive "gs://${BUCKET}/${STAGE_PREFIX}"
gcloud transfer jobs delete "$JOB_NAME" --quiet
```
