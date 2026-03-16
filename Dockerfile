# Build argument for base image selection
ARG BASE_IMAGE=nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04

# Stage 1: Base image with common dependencies
FROM ${BASE_IMAGE} AS base

# Build arguments for this stage with sensible defaults for standalone builds
ARG COMFYUI_VERSION=latest
ARG CUDA_VERSION_FOR_COMFY
ARG ENABLE_PYTORCH_UPGRADE=false
ARG PYTORCH_INDEX_URL

# Prevents prompts from packages asking for user input during installation
ENV DEBIAN_FRONTEND=noninteractive
# Prefer binary wheels over source distributions for faster pip installations
ENV PIP_PREFER_BINARY=1
# Ensures output from python is printed immediately to the terminal without buffering
ENV PYTHONUNBUFFERED=1
# Speed up some cmake builds
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

# Install Python, git and other necessary tools
RUN apt-get update && apt-get install -y \
    python3.12 \
    python3.12-venv \
    python3-pip \
    git \
    wget \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# Clean up to reduce image size
RUN apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install uv (latest) using official installer and create isolated venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

# Use the virtual environment for all subsequent commands
ENV PATH="/opt/venv/bin:${PATH}"

# Install comfy-cli + dependencies needed by it to install ComfyUI
RUN uv pip install comfy-cli pip setuptools wheel

# Install ComfyUI
RUN if [ -n "${CUDA_VERSION_FOR_COMFY}" ]; then \
      /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --cuda-version "${CUDA_VERSION_FOR_COMFY}" --nvidia; \
    else \
      /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --nvidia; \
    fi

# Upgrade PyTorch if needed (for newer CUDA versions)
RUN if [ "$ENABLE_PYTORCH_UPGRADE" = "true" ]; then \
      uv pip install --force-reinstall torch torchvision torchaudio --index-url ${PYTORCH_INDEX_URL}; \
    fi

# Change working directory to ComfyUI
WORKDIR /comfyui

# Support for the network volume
ADD src/extra_model_paths.yaml ./

# Go back to the root
WORKDIR /

# Install Python runtime dependencies for the handler
RUN uv pip install runpod requests websocket-client

# Add application code and scripts
ADD src/start.sh src/network_volume.py handler.py test_input.json ./
RUN chmod +x /start.sh

# Add script to install custom nodes
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

# Prevent pip from asking for confirmation during uninstall steps in custom nodes
ENV PIP_NO_INPUT=1

# Copy helper script to switch Manager network mode at container start
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# Set the default command to run when starting the container
CMD ["/start.sh"]

# Stage 2: Download models
FROM base AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
# Set default model type if none is provided
ARG MODEL_TYPE=enhance

# Change working directory to ComfyUI
WORKDIR /comfyui

# Create necessary directories upfront
RUN mkdir -p \
    /comfyui/models/checkpoints \
    /comfyui/models/vae \
    /comfyui/models/unet \
    /comfyui/models/clip \
    /comfyui/models/text_encoders \
    /comfyui/models/diffusion_models \
    /comfyui/models/model_patches \
    /comfyui/models/depthanything \
    /comfyui/models/SEEDVR2 \
    /comfyui/models/upscale_models \
    /comfyui/models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF \
    /comfyui/models/ultralytics/segm \
    /comfyui/models/ultralytics/bbox \
    /comfyui/models/sams

RUN if [ "$MODEL_TYPE" = "sd3" ]; then \
      wget -q --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" -O models/checkpoints/sd3_medium_incl_clips_t5xxlfp8.safetensors https://huggingface.co/stabilityai/stable-diffusion-3-medium/resolve/main/sd3_medium_incl_clips_t5xxlfp8.safetensors; \
    fi

RUN if [ "$MODEL_TYPE" = "flux1-schnell" ]; then \
      wget -q --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" -O models/unet/flux1-schnell.safetensors https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/flux1-schnell.safetensors && \
      wget -q -O models/clip/clip_l.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors && \
      wget -q -O models/clip/t5xxl_fp8_e4m3fn.safetensors https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors && \
      wget -q --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" -O models/vae/ae.safetensors https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/ae.safetensors; \
    fi

RUN set -eux; \
    if [ "$MODEL_TYPE" = "enhance" ]; then \
      wget -nv -O /comfyui/models/checkpoints/epicrealism_naturalSinRC1VAE.safetensors \
        https://huggingface.co/philz1337x/epicrealism/resolve/main/epicrealism_naturalSinRC1VAE.safetensors; \
      wget -nv -O /comfyui/models/diffusion_models/svdq-fp4_r32-flux.1-dev.safetensors \
        https://huggingface.co/nunchaku-ai/nunchaku-flux.1-dev/resolve/main/svdq-fp4_r32-flux.1-dev.safetensors; \
      wget -nv -O /comfyui/models/diffusion_models/svdq-int4_r32-flux.1-dev.safetensors \
        https://huggingface.co/nunchaku-ai/nunchaku-flux.1-dev/resolve/main/svdq-int4_r32-flux.1-dev.safetensors; \
      wget -nv -O /comfyui/models/diffusion_models/svdq-fp4_r32-fluxmania-legacy.safetensors \
        https://huggingface.co/spooknik/Fluxmania-SVDQ/resolve/main/svdq-fp4_r32-fluxmania-legacy.safetensors; \
      wget -nv -O /comfyui/models/diffusion_models/svdq-int4_r32-fluxmania-legacy.safetensors \
        https://huggingface.co/spooknik/Fluxmania-SVDQ/resolve/main/svdq-int4_r32-fluxmania-legacy.safetensors; \
    fi

RUN set -eux; \
    if [ "$MODEL_TYPE" = "enhance" ]; then \
      wget -nv -O /comfyui/models/loras/detailSliderALT2.safetensors \
        https://huggingface.co/iamanaiart/flatloras/resolve/main/detailSliderALT2.safetensors; \
      wget -nv -O /comfyui/models/loras/boreal-flux-dev-lora-v04_1000_steps.safetensors \
        https://huggingface.co/kudzueye/Boreal/resolve/main/boreal-flux-dev-lora-v04_1000_steps.safetensors; \
      wget -nv -O /comfyui/models/loras/flux-RealismLora.safetensors \
        https://huggingface.co/XLabs-AI/flux-RealismLora/resolve/main/lora.safetensors; \
    fi

RUN set -eux; \
    if [ "$MODEL_TYPE" = "enhance" ]; then \
      wget -nv -O /comfyui/models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF/Qwen3VL-4B-Instruct-Q8_0.gguf \
        https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q8_0.gguf; \
      wget -nv -O /comfyui/models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF/mmproj-Qwen3VL-4B-Instruct-F16.gguf \
        https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-F16.gguf; \
      wget -nv -O /comfyui/models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf \
        https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf; \
    fi
RUN set -eux; \
    if [ "$MODEL_TYPE" = "enhance" ]; then \
      wget -nv -O /comfyui/models/upscale_models/1x-ReFocus-V3.pth \
        https://huggingface.co/notkenski/upscalers/resolve/main/1x-ReFocus-V3.pth; \
      wget -nv -O /comfyui/models/sams/sam_vit_b_01ec64.pth \
        https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth; \
      wget -nv -O /comfyui/models/ultralytics/bbox/face_yolov8m.pt \
        https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt; \
      wget -nv -O /comfyui/models/ultralytics/segm/person_yolov8m-seg.pt \
        https://huggingface.co/Bingsu/adetailer/resolve/main/person_yolov8m-seg.pt; \
      wget -nv -O /comfyui/models/ultralytics/segm/face_yolov8m-seg_60.pt \
        https://github.com/hben35096/assets/releases/download/yolo8/face_yolov8m-seg_60.pt; \
    fi

RUN set -eux; \
    if [ "$MODEL_TYPE" = "enhance" ]; then \
      wget -nv --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" \
        -O /comfyui/models/vae/ae.safetensors \
        https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/ae.safetensors; \
      wget -nv -O /comfyui/models/text_encoders/clip_l.safetensors \
        https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors; \
      wget -nv -O /comfyui/models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors \
        https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors; \
    fi

RUN if [ "$MODEL_TYPE" = "flux1-dev" ]; then \
      wget -q -O models/checkpoints/flux1-dev-fp8.safetensors https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors; \
    fi

RUN if [ "$MODEL_TYPE" = "z-image-turbo" ]; then \
      wget -q -O models/text_encoders/qwen_3_4b.safetensors https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors && \
      wget -q -O models/diffusion_models/z_image_turbo_bf16.safetensors https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors && \
      wget -q -O models/vae/ae.safetensors https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors && \
      wget -q -O models/model_patches/Z-Image-Turbo-Fun-Controlnet-Union.safetensors https://huggingface.co/alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union/resolve/main/Z-Image-Turbo-Fun-Controlnet-Union.safetensors; \
    fi

RUN if [ "$MODEL_TYPE" = "seedvr" ]; then \
  # Flux text encoders (clip_l + t5xxl scaled)
  wget -q -O models/text_encoders/clip_l.safetensors \
    "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors" && \
  wget -q -O models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors \
    "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn_scaled.safetensors" && \

  # Flux VAE (GATED repo -> needs HF token with access)
  wget -q --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" \
    -O models/vae/ae.safetensors \
    "https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/ae.safetensors" && \

  # SeedVR2 models (saved to ComfyUI/models/SEEDVR2 as per project docs)
  wget -q -O models/SEEDVR2/ema_vae_fp16.safetensors \
    "https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/ema_vae_fp16.safetensors" && \
  wget -q -O models/SEEDVR2/seedvr2_ema_7b_sharp_fp8_e4m3fn.safetensors \
    "https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/seedvr2_ema_7b_sharp_fp8_e4m3fn.safetensors" && \

  # Upscaler model (goes in models/upscale_models)
  wget -q -O models/upscale_models/4xNomos8kDAT.pth \
    "https://huggingface.co/uwg/upscaler/resolve/main/ESRGAN/4xNomos8kDAT.pth" && \

  # Fluxmania SVDQ fp4 (explicitly recommended for Blackwell/RTX 50-series)
  wget -q -O models/diffusion_models/svdq-fp4_r32-fluxmania-legacy.safetensors \
    "https://huggingface.co/spooknik/Fluxmania-SVDQ/resolve/main/svdq-fp4_r32-fluxmania-legacy.safetensors" ; \
fi


RUN if [ "$MODEL_TYPE" = "flux2-klein" ]; then \
  wget -q -O models/depthanything/depth_anything_v2_vitl_fp16.safetensors \
    "https://huggingface.co/Kijai/DepthAnythingV2-safetensors/resolve/main/depth_anything_v2_vitl_fp16.safetensors?download=true" && \
  wget -q --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" -O models/diffusion_models/flux-2-klein-9b-fp8.safetensors \
    "https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8/resolve/main/flux-2-klein-9b-fp8.safetensors" && \
  wget -q --header="Authorization: Bearer ${HUGGINGFACE_ACCESS_TOKEN}" -O models/diffusion_models/flux-2-klein-base-9b-fp8.safetensors \
    "https://huggingface.co/black-forest-labs/FLUX.2-klein-base-9b-fp8/resolve/main/flux-2-klein-base-9b-fp8.safetensors" && \
  wget -q -O models/text_encoders/qwen_3_8b_fp8mixed.safetensors \
    "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors?download=true" && \
  wget -q -O models/vae/flux2-vae.safetensors \
    "https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/resolve/main/split_files/vae/flux2-vae.safetensors?download=true" && \
  wget -q -O models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF/Qwen3VL-4B-Instruct-Q8_0.gguf \
    "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q8_0.gguf"; \
  wget -q -O models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF/mmproj-Qwen3VL-4B-Instruct-F16.gguf \
    "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-F16.gguf"; \
  wget -q -O models/llm/GGUF/Qwen/Qwen3-VL-4B-Instruct-GGUF/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf \
    "https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"; \
fi


# Stage 3: Final image
FROM base AS final

# Copy models from stage 2 to the final image
COPY --from=downloader /comfyui/models /comfyui/models

# --- NEW: flux2-klein image variant with bundled LoRAs ---
FROM final AS final-flux2-klein

# Make sure the folder exists
RUN mkdir -p /comfyui/models/loras

# Copy LoRAs from build context into the image
COPY ./models/loras/klein9bDetailSlider.Xrt1.safetensors /comfyui/models/loras/
COPY ./models/loras/klein9bRealismSlider.U3P5.safetensors /comfyui/models/loras/
COPY ./models/loras/Klein_ref_transfer_02.safetensors /comfyui/models/loras/
COPY ./models/loras/lenovo_flux_klein9b.safetensors /comfyui/models/loras/
COPY ./models/loras/reccam_Klein_v01.safetensors /comfyui/models/loras/
COPY ./models/loras/Klein-consistency.safetensors /comfyui/models/loras/
COPY ./models/loras/realistic.safetensors /comfyui/models/loras/

# Need curl for GitHub API (base image installs wget but not curl)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*


# Install nodes via comfy-cli (registry)
RUN comfy-node-install \
  comfyui-depthanythingv2 \
  ComfyUI-QwenVL \
  comfyui-custom-scripts


# ---------- llama-cpp-python (Vision / Qwen-VL GGUF) ----------
SHELL ["/bin/bash", "-lc"]

# Defaults (override via build args in docker-bake.hcl)
ARG LLAMA_CPP_PYTHON_REPO=JamePeng/llama-cpp-python
ARG LLAMA_CPP_PYTHON_TAG=v0.3.30-cu128-Basic-linux-20260302
# Repo base uses python3.12 -> cp312 :contentReference[oaicite:2]{index=2}
ARG LLAMA_CPP_PYTHON_PYTAG=cp312

# Export to the script environment (because heredoc is single-quoted)
ENV LLAMA_CPP_PYTHON_REPO="${LLAMA_CPP_PYTHON_REPO}" \
    LLAMA_CPP_PYTHON_TAG="${LLAMA_CPP_PYTHON_TAG}" \
    LLAMA_CPP_PYTHON_PYTAG="${LLAMA_CPP_PYTHON_PYTAG}"

RUN cat > /tmp/install_llama_vision.sh <<'SH'
set -eux
mkdir -p /opt/wheels

API="https://api.github.com/repos/${LLAMA_CPP_PYTHON_REPO}/releases/tags/${LLAMA_CPP_PYTHON_TAG}"
curl -sL "$API" -o /tmp/llama_release.json

# --- pick wheel name + url from release assets ---
WHEEL_LINE="$(python - <<'PY'
import json, os, sys
data = json.load(open('/tmp/llama_release.json', 'r', encoding='utf-8'))
assets = data.get('assets', [])
pytag = os.environ.get('LLAMA_CPP_PYTHON_PYTAG', 'cp312')

cand = []
for a in assets:
    name = a.get('name','')
    url  = a.get('browser_download_url','')
    if not name.endswith('.whl'):
        continue
    if pytag not in name:
        continue
    if not (('linux_x86_64' in name) or ('manylinux' in name)):
        continue
    # prefer CUDA wheels if multiple match
    cand.append((('cu' in name), len(name), name, url))

if not cand:
    print("No matching wheel found.", file=sys.stderr)
    print("Assets:", [a.get('name') for a in assets], file=sys.stderr)
    sys.exit(1)

cand.sort(reverse=True)
name, url = cand[0][2], cand[0][3]
print(name + "\t" + url)
PY
)"

WHEEL_NAME="$(printf '%s' "$WHEEL_LINE" | cut -f1)"
WHEEL_URL="$(printf '%s' "$WHEEL_LINE" | cut -f2-)"
WHEEL_PATH="/opt/wheels/$WHEEL_NAME"

echo "Wheel path: $WHEEL_PATH"
if [ ! -f "$WHEEL_PATH" ]; then
  echo "Downloading: $WHEEL_NAME"
  curl -L "$WHEEL_URL" -o "$WHEEL_PATH"
else
  echo "Using cached wheel: $WHEEL_NAME"
fi

# venv is already active via PATH=/opt/venv/bin:$PATH :contentReference[oaicite:3]{index=3}
/opt/venv/bin/python -m pip install --no-cache-dir --force-reinstall "$WHEEL_PATH"

# verify handlers exist WITHOUT importing llama_cpp (avoid CUDA load at build time)
python - <<'PY'
import site, pathlib
target = None
for sp in site.getsitepackages():
    cand = pathlib.Path(sp) / "llama_cpp" / "llama_chat_format.py"
    if cand.exists():
        target = cand
        break

if not target:
    raise SystemExit("Could not find llama_cpp/llama_chat_format.py in site-packages")

txt = target.read_text(encoding="utf-8", errors="ignore")
needed = ["Qwen3VLChatHandler", "Qwen25VLChatHandler"]
missing = [n for n in needed if n not in txt]
if missing:
    raise SystemExit(f"Missing handlers in {target}: {missing}")

print(f"OK: found {needed} in {target} (no CUDA import during build)")
PY

rm -f /tmp/llama_release.json
SH


RUN --mount=type=cache,target=/opt/wheels \
    tr -d '\r' < /tmp/install_llama_vision.sh > /tmp/install_llama_vision.lf \
 && mv /tmp/install_llama_vision.lf /tmp/install_llama_vision.sh \
 && bash /tmp/install_llama_vision.sh \
 && rm -f /tmp/install_llama_vision.sh




 # --- NEW: seedvr image variant ---
FROM final AS final-seedvr

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install nodes via comfy-cli (registry)
RUN comfy-node-install \
  seedvr2_videoupscaler \
  rgthree-comfy \
  comfyui-custom-scripts \
  ComfyUI-DyPE \
  comfyui_ultimatesdupscale

# ComfyUI-nunchaku plugin pinned
RUN rm -rf /comfyui/custom_nodes/ComfyUI-nunchaku && \
    git clone --depth 1 --branch v1.2.1 https://github.com/nunchaku-ai/ComfyUI-nunchaku /comfyui/custom_nodes/ComfyUI-nunchaku && \
    /opt/venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-nunchaku/requirements.txt

# Offline versions file (prevents "minimal mode" warning)
RUN curl -fsSL https://nunchaku.tech/cdn/nunchaku_versions.json \
  -o /comfyui/custom_nodes/ComfyUI-nunchaku/nunchaku_versions.json

# --- Nunchaku backend (THIS is what provides `import nunchaku`) ---
# Must match: cu12.8 + torch2.10 + cp312
ARG NUNCHAKU_WHEEL_URL=https://github.com/nunchaku-ai/nunchaku/releases/download/v1.2.1/nunchaku-1.2.1+cu12.8torch2.10-cp312-cp312-linux_x86_64.whl
RUN /opt/venv/bin/python -m pip install --no-cache-dir ${NUNCHAKU_WHEEL_URL} && \
    /opt/venv/bin/python -c "import nunchaku, importlib.metadata as m; print('nunchaku OK:', m.version('nunchaku'))"

# Essentials pinned
ARG ESSENTIALS_COMMIT=9d9f4bedfc9f0321c19faf71855e228c93bd0dc9
RUN rm -rf /comfyui/custom_nodes/ComfyUI_essentials \
 && mkdir -p /comfyui/custom_nodes/ComfyUI_essentials \
 && git init /comfyui/custom_nodes/ComfyUI_essentials \
 && cd /comfyui/custom_nodes/ComfyUI_essentials \
 && git remote add origin https://github.com/cubiq/ComfyUI_essentials \
 && git fetch --depth 1 origin ${ESSENTIALS_COMMIT} \
 && git checkout FETCH_HEAD \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi

# WAS pinned
ARG WAS_COMMIT=ea935d1044ae5a26efa54ebeb18fe9020af49a45
RUN rm -rf /comfyui/custom_nodes/was-node-suite-comfyui \
 && mkdir -p /comfyui/custom_nodes/was-node-suite-comfyui \
 && git init /comfyui/custom_nodes/was-node-suite-comfyui \
 && cd /comfyui/custom_nodes/was-node-suite-comfyui \
 && git remote add origin https://github.com/WASasquatch/was-node-suite-comfyui.git \
 && git fetch --depth 1 origin ${WAS_COMMIT} \
 && git checkout FETCH_HEAD \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi




 # --- NEW: enhance image variant ---


FROM final AS final-enhance

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------
# Custom nodes needed by the workflow
# --------------------------------------------------

# Registry installs
RUN comfy-node-install \
  rgthree-comfy \
  comfyui-custom-scripts \
  comfyui-impact-pack \
  comfyui-impact-subpack \
  comfyui-easy-use

# Essentials pinned
ARG ESSENTIALS_COMMIT=9d9f4bedfc9f0321c19faf71855e228c93bd0dc9
RUN rm -rf /comfyui/custom_nodes/ComfyUI_essentials \
 && mkdir -p /comfyui/custom_nodes/ComfyUI_essentials \
 && git init /comfyui/custom_nodes/ComfyUI_essentials \
 && cd /comfyui/custom_nodes/ComfyUI_essentials \
 && git remote add origin https://github.com/cubiq/ComfyUI_essentials \
 && git fetch --depth 1 origin ${ESSENTIALS_COMMIT} \
 && git checkout FETCH_HEAD \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi

# WAS pinned
ARG WAS_COMMIT=ea935d1044ae5a26efa54ebeb18fe9020af49a45
RUN rm -rf /comfyui/custom_nodes/was-node-suite-comfyui \
 && mkdir -p /comfyui/custom_nodes/was-node-suite-comfyui \
 && git init /comfyui/custom_nodes/was-node-suite-comfyui \
 && cd /comfyui/custom_nodes/was-node-suite-comfyui \
 && git remote add origin https://github.com/WASasquatch/was-node-suite-comfyui.git \
 && git fetch --depth 1 origin ${WAS_COMMIT} \
 && git checkout FETCH_HEAD \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi

# QwenVL
ARG QWENVL_COMMIT=main
RUN rm -rf /comfyui/custom_nodes/ComfyUI-QwenVL \
 && git clone https://github.com/1038lab/ComfyUI-QwenVL /comfyui/custom_nodes/ComfyUI-QwenVL \
 && cd /comfyui/custom_nodes/ComfyUI-QwenVL \
 && if [ "${QWENVL_COMMIT}" != "main" ]; then git checkout ${QWENVL_COMMIT}; fi \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi

# Tooling nodes
ARG TOOLING_COMMIT=main
RUN rm -rf /comfyui/custom_nodes/comfyui-tooling-nodes \
 && git clone https://github.com/Acly/comfyui-tooling-nodes.git /comfyui/custom_nodes/comfyui-tooling-nodes \
 && cd /comfyui/custom_nodes/comfyui-tooling-nodes \
 && if [ "${TOOLING_COMMIT}" != "main" ]; then git checkout ${TOOLING_COMMIT}; fi \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi

# Post-processing nodes
ARG POST_COMMIT=main
RUN rm -rf /comfyui/custom_nodes/ComfyUI-post-processing-nodes \
 && git clone https://github.com/EllangoK/ComfyUI-post-processing-nodes.git /comfyui/custom_nodes/ComfyUI-post-processing-nodes \
 && cd /comfyui/custom_nodes/ComfyUI-post-processing-nodes \
 && if [ "${POST_COMMIT}" != "main" ]; then git checkout ${POST_COMMIT}; fi \
 && if [ -f requirements.txt ]; then /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt; fi

# --------------------------------------------------
# ComfyUI-Nunchaku
# --------------------------------------------------

ARG NUNCHAKU_COMFYUI_TAG=v1.2.1
RUN rm -rf /comfyui/custom_nodes/ComfyUI-nunchaku \
 && git clone --depth 1 --branch ${NUNCHAKU_COMFYUI_TAG} https://github.com/nunchaku-ai/ComfyUI-nunchaku /comfyui/custom_nodes/ComfyUI-nunchaku \
 && if [ -f /comfyui/custom_nodes/ComfyUI-nunchaku/requirements.txt ]; then \
      /opt/venv/bin/python -m pip install --no-cache-dir -r /comfyui/custom_nodes/ComfyUI-nunchaku/requirements.txt; \
    fi

# Offline versions file
RUN curl -fsSL https://nunchaku.tech/cdn/nunchaku_versions.json \
  -o /comfyui/custom_nodes/ComfyUI-nunchaku/nunchaku_versions.json

# Nunchaku backend wheel
ARG NUNCHAKU_WHEEL_URL=https://github.com/nunchaku-ai/nunchaku/releases/download/v1.2.1/nunchaku-1.2.1+cu12.8torch2.10-cp312-cp312-linux_x86_64.whl
RUN /opt/venv/bin/python -m pip install --no-cache-dir ${NUNCHAKU_WHEEL_URL} \
 && /opt/venv/bin/python -c "import nunchaku, importlib.metadata as m; print('nunchaku OK:', m.version('nunchaku'))"

# --------------------------------------------------
# llama-cpp-python (Vision / Qwen-VL GGUF)
# --------------------------------------------------

SHELL ["/bin/bash", "-lc"]

ARG LLAMA_CPP_PYTHON_REPO=JamePeng/llama-cpp-python
ARG LLAMA_CPP_PYTHON_TAG=v0.3.30-cu128-Basic-linux-20260302
ARG LLAMA_CPP_PYTHON_PYTAG=cp312

ENV LLAMA_CPP_PYTHON_REPO="${LLAMA_CPP_PYTHON_REPO}" \
    LLAMA_CPP_PYTHON_TAG="${LLAMA_CPP_PYTHON_TAG}" \
    LLAMA_CPP_PYTHON_PYTAG="${LLAMA_CPP_PYTHON_PYTAG}"

RUN cat > /tmp/install_llama_vision.sh <<'SH'
set -eux
mkdir -p /opt/wheels

API="https://api.github.com/repos/${LLAMA_CPP_PYTHON_REPO}/releases/tags/${LLAMA_CPP_PYTHON_TAG}"
curl -sL "$API" -o /tmp/llama_release.json

WHEEL_LINE="$(python - <<'PY'
import json, os, sys
data = json.load(open('/tmp/llama_release.json', 'r', encoding='utf-8'))
assets = data.get('assets', [])
pytag = os.environ.get('LLAMA_CPP_PYTHON_PYTAG', 'cp312')

cand = []
for a in assets:
    name = a.get('name', '')
    url  = a.get('browser_download_url', '')
    if not name.endswith('.whl'):
        continue
    if pytag not in name:
        continue
    if not (('linux_x86_64' in name) or ('manylinux' in name)):
        continue
    cand.append((('cu' in name), len(name), name, url))

if not cand:
    print("No matching wheel found.", file=sys.stderr)
    print("Assets:", [a.get('name') for a in assets], file=sys.stderr)
    sys.exit(1)

cand.sort(reverse=True)
name, url = cand[0][2], cand[0][3]
print(name + "\t" + url)
PY
)"

WHEEL_NAME="$(printf '%s' "$WHEEL_LINE" | cut -f1)"
WHEEL_URL="$(printf '%s' "$WHEEL_LINE" | cut -f2-)"
WHEEL_PATH="/opt/wheels/$WHEEL_NAME"

echo "Wheel path: $WHEEL_PATH"
if [ ! -f "$WHEEL_PATH" ]; then
  echo "Downloading: $WHEEL_NAME"
  curl -L "$WHEEL_URL" -o "$WHEEL_PATH"
else
  echo "Using cached wheel: $WHEEL_NAME"
fi

/opt/venv/bin/python -m pip install --no-cache-dir --force-reinstall "$WHEEL_PATH"

python - <<'PY'
import site, pathlib
target = None
for sp in site.getsitepackages():
    cand = pathlib.Path(sp) / "llama_cpp" / "llama_chat_format.py"
    if cand.exists():
        target = cand
        break

if not target:
    raise SystemExit("Could not find llama_cpp/llama_chat_format.py in site-packages")

txt = target.read_text(encoding="utf-8", errors="ignore")
needed = ["Qwen3VLChatHandler", "Qwen25VLChatHandler"]
missing = [n for n in needed if n not in txt]
if missing:
    raise SystemExit(f"Missing handlers in {target}: {missing}")

print(f"OK: found {needed} in {target} (no CUDA import during build)")
PY

rm -f /tmp/llama_release.json
SH

RUN --mount=type=cache,target=/opt/wheels \
    tr -d '\r' < /tmp/install_llama_vision.sh > /tmp/install_llama_vision.lf \
 && mv /tmp/install_llama_vision.lf /tmp/install_llama_vision.sh \
 && bash /tmp/install_llama_vision.sh \
 && rm -f /tmp/install_llama_vision.sh
