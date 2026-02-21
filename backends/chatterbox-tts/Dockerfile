# Chatterbox TTS — GPU build for RTX 5090 (Blackwell / sm_120)
#
# The upstream image (ghcr.io/devnen/chatterbox-tts-server) ships torch 2.5.1+cu121
# which lacks sm_120 kernels. This build installs PyTorch 2.10+cu128 first,
# then chatterbox-tts with --no-deps to prevent downgrade.
#
# Build on forge (x86_64):
#   sudo docker build -t registry.arunlabs.com/chatterbox-tts-server:gpu-sm120 \
#       backends/chatterbox-tts/
#   sudo docker push registry.arunlabs.com/chatterbox-tts-server:gpu-sm120

FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

# System deps (matches upstream)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libsndfile1 \
        ffmpeg \
        python3 \
        python3-pip \
        python3-dev \
        python3-venv \
        git \
    && ln -s /usr/bin/python3 /usr/bin/python \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --upgrade pip

# ── Step 1: PyTorch 2.10 + CUDA 12.8 (sm_120 support) ──
RUN pip3 install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu128

# ── Step 2: Clone upstream server ──
RUN git clone --depth 1 https://github.com/devnen/Chatterbox-TTS-Server.git /app

WORKDIR /app

# ── Step 3: chatterbox-tts deps (everything EXCEPT torch/torchaudio) ──
# These are chatterbox's actual deps from pyproject.toml.
# We install them first so --no-deps on chatterbox only skips torch.
RUN pip3 install --no-cache-dir \
    numpy \
    "librosa==0.11.0" \
    s3tokenizer \
    "transformers==4.46.3" \
    "diffusers==0.29.0" \
    "resemble-perth==1.0.1" \
    "conformer==0.3.2" \
    "safetensors==0.5.3" \
    spacy-pkuseg \
    "pykakasi==2.3.0" \
    pyloudnorm \
    omegaconf \
    soundfile

# ── Step 4: descript-audio-codec (server dep, heavy dep tree) ──
RUN pip3 install --no-cache-dir descript-audio-codec

# ── Step 5: chatterbox-tts engine (devnen fork, --no-deps prevents torch downgrade) ──
RUN pip3 install --no-cache-dir --no-deps \
    git+https://github.com/devnen/chatterbox-v2.git@master

# ── Step 6: Web framework + server utilities ──
RUN pip3 install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    PyYAML \
    python-multipart \
    requests \
    Jinja2 \
    watchdog \
    aiofiles \
    unidecode \
    inflect \
    tqdm \
    hf_transfer \
    pydub \
    audiotsm \
    praat-parselmouth \
    onnx==1.16.0

# ── Step 7: Patch chatterbox for PyTorch 2.10 dtype strictness ──
# torch 2.10 rejects implicit float64→float32 casts that older versions allowed.
# Patch 1: s3tokenizer mel spectrogram matmul
RUN sed -i 's/self._mel_filters.to(self.device) @ magnitudes/self._mel_filters.to(self.device) @ magnitudes.float()/' \
    /usr/local/lib/python3.10/dist-packages/chatterbox/models/s3tokenizer/s3tokenizer.py
# Patch 2: voice_encoder LSTM expects float32 input
RUN sed -i 's/_, (hidden, _) = self.lstm(mels)/_, (hidden, _) = self.lstm(mels.float())/' \
    /usr/local/lib/python3.10/dist-packages/chatterbox/models/voice_encoder/voice_encoder.py

# Create required directories
RUN mkdir -p model_cache reference_audio outputs voices logs hf_cache

EXPOSE 8004

CMD ["python3", "server.py"]
