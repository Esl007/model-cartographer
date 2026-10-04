"""Serve gemma-4-31b-it-qat-w4a16-ct on Modal as an OpenAI-compatible endpoint.

Cartographer treats this endpoint as just another provider (see
`modal_provider` in cartographer/exec/client.py), so every province runs
against the exact competition checkpoint while the Nemotron roles
(renderer, proposer, analyst) stay on Nebius.

One-time setup, in order:

  1. pip install modal
     modal setup                  # browser auth; writes ~/.modal.toml

  2. Kaggle credentials (the weights live on Kaggle Models):
     kaggle.com -> Settings -> API -> Create New Token, then
     modal secret create kaggle-api \
         KAGGLE_USERNAME=<username> KAGGLE_KEY=<key>

  3. An API key of our choosing that protects the endpoint:
     modal secret create gemma4-endpoint \
         VLLM_API_KEY=$(python3 -c "import secrets;print('ct-'+secrets.token_urlsafe(24))")
     (keep the printed value; it goes in env/"Nebius api.txt", step 6)

  4. Confirm MODEL_HANDLE below against the model's Kaggle page
     (kagglehub shows the exact handle on the page), then download the
     weights into the Modal volume (one-off, CPU only, no GPU cost):
     modal run scripts/modal_serve_gemma4.py::download_weights

  5. Deploy the server:
     modal deploy scripts/modal_serve_gemma4.py
     Modal prints the public URL, e.g.
     https://<workspace>--gemma4-qat-serve.modal.run
     Smoke-test it (first call cold-starts the GPU, allow a few minutes):
     curl -H "Authorization: Bearer <VLLM_API_KEY>" \
          https://<workspace>--gemma4-qat-serve.modal.run/v1/models

  6. Register it with Cartographer -- add ONE line to env/"Nebius api.txt":
     modal <VLLM_API_KEY> https://<workspace>--gemma4-qat-serve.modal.run/v1
     Then /launch lists provider "modal", and:
     python3 scripts/run_all_provinces.py --provider modal \
         --model gemma-4-31b-it-qat-w4a16-ct --label gemma4qat --smoke

Costs: one L40S (~$2/h) ONLY while a request keeps the container warm;
it scales to zero after SCALEDOWN_S idle. The weights volume (~18 GB)
is a few cents/month. A full map (~$1 of tokens on Nebius pricing) is
roughly 3-5 GPU-hours here depending on parallelism.
"""

from __future__ import annotations

import os
import subprocess

import modal

# ---------------------------------------------------------------- config

# Primary source: the checkpoint is public and ungated on Hugging Face
# (verified 2026-10-03: ~23 GB, no license wall), so no token is needed.
HF_REPO = os.environ.get("GEMMA4_HF_REPO", "google/gemma-4-31B-it-qat-w4a16-ct")
# Kaggle fallback, used only when GEMMA4_HF_REPO is set to empty: the
# public google/gemma-4 Kaggle model has no w4a16-ct variation (checked
# 2026-10-03), so this handle stays a placeholder for a future upload.
MODEL_HANDLE = os.environ.get(
    "GEMMA4_KAGGLE_HANDLE",
    "google/gemma-4/transformers/gemma-4-31b-it-qat-w4a16-ct",
)

SERVED_NAME = "gemma-4-31b-it-qat-w4a16-ct"  # model id clients ask for
GPU = "L40S"            # 48 GB; ~18 GB weights + KV headroom
MAX_MODEL_LEN = 32768   # province tasks are small; keeps KV cache modest
PORT = 8000
SCALEDOWN_S = 300       # idle minutes before the GPU is released
WEIGHTS_DIR = "/weights"

app = modal.App("gemma4-qat")

volume = modal.Volume.from_name("gemma4-weights", create_if_missing=True)

image = (
    # CUDA *devel* base, not debian_slim: vLLM's flashinfer JIT-compiles
    # kernels at engine startup and dies without nvcc ("Could not find
    # nvcc and default cuda_home='/usr/local/cuda' doesn't exist" --
    # exactly what the first deploy's logs showed, 2026-10-03).
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    # Latest vLLM on purpose: Gemma 4 (Apr 2026) needs a 2026 vLLM. Pin
    # the version here as soon as one deploy works, so rebuilds stay
    # reproducible.
    .pip_install("vllm", "kagglehub", "huggingface_hub")
)


# ------------------------------------------------- one-off weight download


@app.function(
    image=image,
    volumes={WEIGHTS_DIR: volume},
    secrets=[modal.Secret.from_name("kaggle-api")],
    timeout=3600,
)
def download_weights() -> str:
    """Pull the checkpoint into the shared volume (CPU container, no GPU)."""
    import pathlib
    import shutil

    dest = pathlib.Path(WEIGHTS_DIR) / "gemma4"
    if (dest / "config.json").exists():
        return f"already present at {dest}"

    if HF_REPO:
        from huggingface_hub import snapshot_download

        snapshot_download(HF_REPO, local_dir=str(dest))
    else:
        import kagglehub

        cache_path = kagglehub.model_download(MODEL_HANDLE)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cache_path, dest, dirs_exist_ok=True)

    volume.commit()
    files = sorted(p.name for p in dest.iterdir())
    return f"downloaded to {dest}: {files}"


# ------------------------------------------------------------- the server


@app.function(
    image=image,
    gpu=GPU,
    volumes={WEIGHTS_DIR: volume},
    secrets=[modal.Secret.from_name("gemma4-endpoint")],
    scaledown_window=SCALEDOWN_S,
    timeout=3600,
    # Exactly one GPU, ever: a full map runs ~12 stages x 4-6 workers in
    # parallel, and without this cap Modal's autoscaler would add a second
    # L40S the moment concurrency passes max_inputs -- doubling the burn.
    # vLLM's own scheduler queues excess requests fine on one card.
    max_containers=1,
)
@modal.concurrent(max_inputs=32)
@modal.web_server(port=PORT, startup_timeout=900)
def serve() -> None:
    """vLLM's OpenAI-compatible server; Modal fronts it at a public URL."""
    cmd = [
        "vllm", "serve", f"{WEIGHTS_DIR}/gemma4",
        "--served-model-name", SERVED_NAME,
        "--host", "0.0.0.0",
        "--port", str(PORT),
        "--max-model-len", str(MAX_MODEL_LEN),
        "--gpu-memory-utilization", "0.92",
        "--api-key", os.environ["VLLM_API_KEY"],
    ]
    subprocess.Popen(cmd)
