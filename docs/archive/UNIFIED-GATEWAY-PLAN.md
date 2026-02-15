# Unified AI Gateway — Execution Plan

> **Status**: Phase 1 complete — gateway deployed, Bifrost deleted, Ollama removed
> **Date**: 2026-02-14
> **Replaces**: Bifrost gateway, LiteLLM proxy (Phase 3 of BUILD-PLAN.md — cancelled)
> **Source analysis**: coqui-tts-server repo (speech engines) + this repo (LLM gateway)
>
> **Source of truth**: [`docs/VOICE-API.md`](VOICE-API.md) — all current endpoint specs, schemas, curl examples, and deployment status live there. This plan doc is the _design spec_ (historical); VOICE-API.md is the _live integration guide_.

---

## Context

Two problems converge into one solution:

1. **coqui-tts-server** is a monolithic GPU pod bundling 4 ML engines (~12GB VRAM) that should be decomposed into independent microservices
2. **Synapse** depends on third-party gateways (Bifrost today, LiteLLM planned) that are overkill for a single-user cluster and don't handle speech protocols

**Decision**: Build one custom FastAPI gateway that replaces both. A single `synapse.arunlabs.com` endpoint routes ALL AI requests — LLM chat, embeddings, TTS, STT, speaker analysis, audio processing — to the right backend. No Bifrost. No LiteLLM. No separate speech gateway. One URL, one service, fully under our control.

**Key Model Change**: XTTS v2 → Chatterbox Turbo (350M params, MIT license, zero-shot voice cloning from 6-second samples, 23 languages, RTX 5090 / CUDA 12.8 ready)

## Architecture

```
Clients (OpenClaw, AIBOM, any future app)
         |
         v
Synapse Gateway (synapse.arunlabs.com)            [llm-infra namespace]
  Custom FastAPI, ~400 lines
  Routes: /v1/chat/completions, /v1/embeddings,
          /tts/*, /stt/*, /speakers/*, /audio/*, /voices/*
  Owns: voice library (/data/voices PVC), routing config, health aggregation
         |
    +----+----+----+--------+--------+-----------+
    v    v    v    v        v        v           v
  llama  vLLM  Ollama  Chatterbox  Whisper  pyannote  DeepFilter
  embed  (GPU) (CPU)   Turbo TTS   STT     Speaker   Audio
  (CPU)               (GPU/CPU)   (CPU)    (CPU)     (CPU)

  All backends in llm-infra namespace, accessed via k8s service DNS
```

**One gateway. One URL. One namespace. All AI services.**

## What Changes vs. Current Synapse

| Current (Bifrost)           | New (Custom Gateway)                     |
| --------------------------- | ---------------------------------------- |
| Bifrost proxies LLM routes  | Custom FastAPI routes LLM + Speech       |
| LiteLLM planned for Phase 3 | Cancelled — not needed                   |
| Speech is separate project  | Speech routes built into the gateway     |
| Voice manager doesn't exist | Voice manager built into gateway         |
| Config via Bifrost JSON     | Config via YAML/env vars                 |
| No speech backends          | 4 speech backends deployed alongside LLM |

---

## Where Code Lives

This work spans **two repos**:

### This repo (Synapse) — Gateway + Manifests

```
synapse/
├── gateway/
│   ├── src/
│   │   ├── __init__.py            # Package init
│   │   ├── main.py                # FastAPI app — lifespan, health, routers (~100 lines)
│   │   ├── config.py              # Load backends.yaml + Pydantic settings (~40 lines)
│   │   ├── backend_client.py      # httpx async client, retry, circuit breaker (~80 lines)
│   │   ├── router_llm.py          # /v1/chat/completions, /v1/embeddings proxy (~60 lines)
│   │   ├── router_tts.py          # /tts/*, /voices/* routes (~120 lines)
│   │   ├── router_stt.py          # /stt/* routes (~60 lines)
│   │   ├── router_speaker.py      # /speakers/* routes (~60 lines)
│   │   ├── router_audio.py        # /audio/* routes (~60 lines)
│   │   ├── models.py              # Pydantic schemas (COPY from coqui-tts-server)
│   │   └── voice_manager.py       # Voice CRUD (COPY from coqui-tts-server, modified)
│   ├── Dockerfile                 # python:3.11-slim, no ML deps
│   └── requirements.txt           # fastapi, uvicorn, httpx, aiofiles, pyyaml
├── config/
│   └── backends.yaml              # Backend registry (REPLACES litellm-config.yaml)
├── manifests/
│   ├── infra/
│   │   ├── namespace.yaml         # llm-infra (exists, unchanged)
│   │   └── pvc-voices.yaml        # 5Gi voice library PVC (NEW)
│   ├── apps/
│   │   ├── gateway.yaml           # Custom gateway (REPLACES bifrost.yaml)
│   │   ├── llama-embed.yaml       # Exists, unchanged
│   │   ├── ollama.yaml            # Exists, unchanged
│   │   ├── chatterbox-tts.yaml    # NEW — Chatterbox Turbo TTS backend
│   │   ├── whisper-stt.yaml       # NEW — faster-whisper STT backend
│   │   ├── pyannote-speaker.yaml  # NEW — pyannote speaker analysis backend
│   │   └── deepfilter-audio.yaml  # NEW — DeepFilterNet audio processing backend
│   └── ingress.yaml               # UPDATE backend from bifrost-gateway to synapse-gateway
└── docs/
    └── UNIFIED-GATEWAY-PLAN.md    # This file
```

### coqui-tts-server repo — Speech Backend Microservices

```
coqui-tts-server/
├── backends/
│   ├── whisper-stt/               # Standalone faster-whisper service
│   │   ├── main.py                # FastAPI app (~120 lines)
│   │   ├── config.py              # Device, model size, compute type
│   │   ├── Dockerfile             # python:3.11-slim + ffmpeg, CPU-only
│   │   └── requirements.txt       # faster-whisper, fastapi, uvicorn, python-multipart
│   ├── pyannote-speaker/          # Standalone pyannote service
│   │   ├── main.py                # FastAPI app (~150 lines)
│   │   ├── config.py              # HF token, device
│   │   ├── Dockerfile             # python:3.11-slim, CPU-only
│   │   └── requirements.txt       # pyannote.audio, fastapi, uvicorn, python-multipart
│   └── deepfilter-audio/          # Standalone DeepFilterNet service
│       ├── main.py                # FastAPI app (~100 lines)
│       ├── config.py              # ffmpeg path
│       ├── Dockerfile             # python:3.11-slim + ffmpeg, CPU-only
│       └── requirements.txt       # deepfilternet, soundfile, fastapi, uvicorn
├── src/                           # Old monolith — DELETE after migration complete
└── ...
```

**Chatterbox TTS** uses `devnen/Chatterbox-TTS-Server` Docker image directly — no custom code needed.

---

## Backend Registry Config

```yaml
# config/backends.yaml — replaces config/litellm-config.yaml
backends:
  # --- LLM ---
  llama-embed:
    url: http://llama-embed.llm-infra.svc.cluster.local:8081
    type: openai-compatible
    health: /health

  # vllm:
  #   url: http://vllm-inference.llm-infra.svc.cluster.local:8001
  #   type: openai-compatible
  #   health: /health

  ollama:
    url: http://ollama.llm-infra.svc.cluster.local:11434
    type: ollama
    health: /api/tags

  # --- Speech ---
  chatterbox-tts:
    url: http://chatterbox-tts.llm-infra.svc.cluster.local:8000
    type: chatterbox
    health: /health

  whisper-stt:
    url: http://whisper-stt.llm-infra.svc.cluster.local:8000
    type: faster-whisper
    health: /health

  pyannote-speaker:
    url: http://pyannote-speaker.llm-infra.svc.cluster.local:8000
    type: pyannote
    health: /health

  deepfilter-audio:
    url: http://deepfilter-audio.llm-infra.svc.cluster.local:8000
    type: deepfilter
    health: /health

# Route mapping
routes:
  /v1/chat/completions: ollama # or vllm when deployed
  /v1/embeddings: llama-embed
  /tts/*: chatterbox-tts
  /stt/*: whisper-stt
  /speakers/*: pyannote-speaker
  /audio/*: deepfilter-audio
```

---

## Critical Source Files Reference

These files from `coqui-tts-server` are the source material for this work:

| Source File (coqui-tts-server)       | Action   | Destination (this repo or coqui backends/) | Notes                                                |
| ------------------------------------ | -------- | ------------------------------------------ | ---------------------------------------------------- |
| `src/models.py` (142 lines)          | Copy     | `gateway/src/models.py`                    | Pydantic schemas, copy unchanged                     |
| `src/voice_manager.py` (304 lines)   | Copy+Fix | `gateway/src/voice_manager.py`             | Remove `tts_engine` import — see fix details below   |
| `src/main.py` (routes)               | Rewrite  | `gateway/src/router_tts.py`                | Extract TTS route logic, proxy to Chatterbox backend |
| `src/stt_engine.py` (154 lines)      | Port     | `backends/whisper-stt/main.py`             | Wrap in standalone FastAPI, CPU mode                 |
| `src/speaker_engine.py` (183 lines)  | Port     | `backends/pyannote-speaker/main.py`        | Wrap in standalone FastAPI, requires HF_TOKEN        |
| `src/audio_processor.py` (203 lines) | Port     | `backends/deepfilter-audio/main.py`        | Wrap in standalone FastAPI, CPU-only                 |
| `src/tts_engine.py` (416 lines)      | DELETE   | N/A — Chatterbox replaces XTTS v2          | Not needed, Chatterbox has its own server            |
| `src/config.py` (32 lines)           | Rewrite  | `gateway/src/config.py`                    | Backend URLs instead of model configs                |

### voice_manager.py Fix Details

When copying `voice_manager.py` to the gateway, make these changes:

**In `add_references()` method (around line 251-252):**

```python
# REMOVE these lines:
from .tts_engine import engine
engine.invalidate_cache(voice_id)
```

**In `delete_voice()` method (around line 284-285):**

```python
# REMOVE these lines:
from .tts_engine import engine
engine.invalidate_cache(voice_id)
```

**Reason**: The gateway doesn't run XTTS v2 locally. Chatterbox handles its own model state. The embedding cache invalidation was specific to the XTTS v2 monolith pattern.

Also update the `__init__` to accept `library_dir` without depending on `settings.voice_library_dir` from the old config — wire it from the new gateway config instead.

### Files to DELETE from Synapse repo

| File                          | Reason                             |
| ----------------------------- | ---------------------------------- |
| `manifests/apps/bifrost.yaml` | Replaced by `gateway.yaml`         |
| `config/litellm-config.yaml`  | Replaced by `config/backends.yaml` |

---

## Phase 1: Custom Gateway + Chatterbox TTS

**Goal**: Replace Bifrost with custom gateway. Prove the pattern with TTS (the hardest endpoint — voice cloning, streaming, file uploads).

### 1a. Build Custom Gateway

**In this repo**, create `gateway/` directory with these files:

**`gateway/src/main.py`** (~100 lines):

- FastAPI app with async lifespan (load backends.yaml, init httpx client pool)
- Include routers: `router_llm`, `router_tts`, `router_stt`, `router_speaker`, `router_audio`
- `GET /health` — aggregate health from all registered backends
- Error handling middleware (catch httpx timeouts → 504, connection errors → 503)

**`gateway/src/config.py`** (~40 lines):

- Pydantic Settings class loading from env vars
- Load `backends.yaml` from configmap mount path
- Key settings: `GATEWAY_CONFIG_PATH`, `VOICE_LIBRARY_DIR`, `LOG_LEVEL`

**`gateway/src/backend_client.py`** (~80 lines):

- Async httpx client pool (connection pooling per backend)
- Retry with exponential backoff (max 3 retries, 0.5s/1s/2s delays)
- Circuit breaker pattern (5 consecutive failures → 30s cooldown → half-open probe)
- Configurable timeouts per backend type:
  - LLM: 300s (streaming completions can be long)
  - TTS: 60s
  - STT: 120s (large audio files)
  - Speaker: 120s (diarization on long audio)
  - Audio: 60s

**`gateway/src/router_llm.py`** (~60 lines):

- `POST /v1/chat/completions` → proxy to Ollama/vLLM (stream SSE if `stream: true`)
- `POST /v1/embeddings` → proxy to llama-embed
- `GET /v1/models` → aggregate model lists from all LLM backends
- Pure HTTP forwarding — read request body, POST to backend, stream response back

**`gateway/src/router_tts.py`** (~120 lines):

- `GET /voices` → voice_manager.list_voices()
- `POST /voices` → voice_manager.upload_voice() (multipart: name + 1-10 WAV files)
- `POST /voices/{voice_id}/references` → voice_manager.add_references()
- `DELETE /voices/{voice_id}` → voice_manager.delete_voice()
- `POST /tts/synthesize` → resolve voice references → POST to Chatterbox with audio files → return WAV
- `POST /tts/stream` → resolve voice → POST to Chatterbox → SSE stream audio chunks
- `POST /tts/interpolate` → resolve multiple voices → POST to Chatterbox → return WAV

**`gateway/src/models.py`** — Copy from `coqui-tts-server/src/models.py` (142 lines, unchanged)

**`gateway/src/voice_manager.py`** — Copy from `coqui-tts-server/src/voice_manager.py` with the tts_engine import fix described above

**`gateway/Dockerfile`**:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER nobody
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**`gateway/requirements.txt`**:

```
fastapi
uvicorn[standard]
httpx
aiofiles
python-multipart
sse-starlette
pydantic-settings
pyyaml
```

### 1b. Deploy Chatterbox TTS Backend

Create `manifests/apps/chatterbox-tts.yaml`:

- Image: `ghcr.io/devnen/chatterbox-tts-server:latest`
  - Has CUDA 12.8 build — compatible with RTX 5090 (Blackwell sm_120)
  - OpenAI-compatible API (`/v1/audio/speech`)
  - Built-in voice cloning from reference audio
- Namespace: `llm-infra`
- GPU: `nvidia.com/gpu: 1` (shared RTX 5090, optional — can fall back to CPU)
- Resources: requests 4Gi/2CPU, limits 16Gi/8CPU
- PVC: 10Gi for model cache (`/root/.cache`)
- Service: `chatterbox-tts:8000`
- Health: `GET /health`
- GPU toleration for `nvidia.com/gpu` NoSchedule

### 1c. Replace Bifrost with Custom Gateway

Create `manifests/apps/gateway.yaml` (replaces `bifrost.yaml`):

- Image: `registry.arunlabs.com/synapse-gateway:latest`
- Resources: requests 256Mi/0.5CPU, limits 1Gi/2CPU (lightweight — no ML deps)
- Volume mounts:
  - PVC `/data/voices` → voice library (new PVC: `pvc-voices.yaml`, 5Gi)
  - ConfigMap `/config/backends.yaml` → routing config
- Service: `synapse-gateway:8000`
- Probes: readiness + liveness on `GET /health`

Create `manifests/infra/pvc-voices.yaml`:

- 5Gi PVC for voice reference samples
- `local-path` storage class

Update `manifests/ingress.yaml`:

- Change backend from `bifrost-gateway:8080` to `synapse-gateway:8000`

DELETE `manifests/apps/bifrost.yaml`

### 1d. Build & Deploy Sequence

```bash
# 1. Build gateway image (from megamind)
cd synapse/gateway
docker build -t registry.arunlabs.com/synapse-gateway:latest .
docker push registry.arunlabs.com/synapse-gateway:latest

# 2. Deploy voice PVC
ssh forge "sudo kubectl apply -f /path/to/manifests/infra/pvc-voices.yaml"

# 3. Deploy Chatterbox TTS backend
ssh forge "sudo kubectl apply -f /path/to/manifests/apps/chatterbox-tts.yaml"

# 4. Deploy custom gateway (replaces Bifrost)
ssh forge "sudo kubectl apply -f /path/to/manifests/apps/gateway.yaml"

# 5. Update ingress
ssh forge "sudo kubectl apply -f /path/to/manifests/ingress.yaml"

# 6. Delete old Bifrost
ssh forge "sudo kubectl delete -f /path/to/manifests/apps/bifrost.yaml"
```

### 1e. Validation

```bash
# LLM still works (same as before, now through custom gateway)
curl -X POST https://synapse.arunlabs.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input": "test", "model": "mxbai-embed-large"}' | jq .

# TTS works (new)
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello world", "language": "en"}' -o test.wav

# Voice upload + cloned synthesis
curl -X POST https://synapse.arunlabs.com/voices \
  -F "name=test-voice" -F "files[]=@reference.wav"

# Health shows all backends
curl https://synapse.arunlabs.com/health | jq .
```

---

## Phase 2: STT Backend (faster-whisper)

### 2a. Create Whisper STT Backend

**In coqui-tts-server repo**, create `backends/whisper-stt/`:

**`main.py`** (~120 lines): FastAPI wrapping faster-whisper

- `POST /transcribe` — file upload → JSON transcript with word-level timestamps
- `POST /detect-language` — file upload → detected language + probability
- `POST /stream` — file upload → SSE streaming segments one-by-one
- `GET /health` — model loaded status

Port inference logic from `coqui-tts-server/src/stt_engine.py`:

- `_load_model()` (line 44-51) — WhisperModel init
- `_transcribe_sync()` (line 62-91) — transcription with word timestamps
- `_detect_language_sync()` (line 100-118) — language detection
- `_stream_sync()` (line 131-151) — streaming segments

Config: `device=cpu`, `compute_type=int8`, `model_size=large-v3-turbo`

**`Dockerfile`**: `python:3.11-slim` + `apt-get install ffmpeg`, CPU-only (no CUDA)

**`requirements.txt`**: `faster-whisper`, `fastapi`, `uvicorn[standard]`, `python-multipart`, `sse-starlette`, `soundfile`, `pydantic-settings`

### 2b. Deploy + Add Gateway Routes

- Build image → push to `registry.arunlabs.com/whisper-stt:latest`
- Create `manifests/apps/whisper-stt.yaml` in Synapse repo:
  - CPU-only, requests 4Gi/2CPU, limits 16Gi/8CPU
  - PVC: 20Gi for model cache (large-v3-turbo is ~3GB)
  - Service: `whisper-stt:8000`
- Add `gateway/src/router_stt.py` (~60 lines):
  - `POST /stt/transcribe` → proxy file upload to whisper-stt `/transcribe`
  - `POST /stt/detect-language` → proxy to `/detect-language`
  - `POST /stt/stream` → proxy to `/stream` (SSE passthrough)
- Update `config/backends.yaml` with whisper-stt entry

### 2c. Validation

```bash
curl -X POST https://synapse.arunlabs.com/stt/transcribe \
  -F "file=@test.wav" | jq .text

curl -X POST https://synapse.arunlabs.com/stt/detect-language \
  -F "file=@test.wav" | jq .detected_language
```

CPU latency target: <2x real-time (10s audio → <20s transcription)

---

## Phase 3: Speaker + Audio Backends

### 3a. pyannote Speaker Backend

**In coqui-tts-server repo**, create `backends/pyannote-speaker/`:

**`main.py`** (~150 lines):

- `POST /diarize` — file upload → who spoke when (segments with speaker labels)
- `POST /verify` — two file uploads → same/different speaker (cosine similarity)
- `GET /health`

Port from `coqui-tts-server/src/speaker_engine.py`:

- `_load_diarization()` (line 52-60)
- `_diarize_sync()` (line 118-152)
- `_load_embedding_model()` (line 86-95)
- `_verify_sync()` (line 164-180)

**Requires**: `HF_TOKEN` env var (pyannote models are gated). User must accept terms at:

- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

### 3b. DeepFilterNet Audio Backend

**In coqui-tts-server repo**, create `backends/deepfilter-audio/`:

**`main.py`** (~100 lines):

- `POST /denoise` — file upload → cleaned WAV (noise reduction)
- `POST /convert` — file upload → different format via ffmpeg
- `GET /health`

Port from `coqui-tts-server/src/audio_processor.py`:

- `_load_denoiser()` (line 51-55) — DeepFilterNet3 init
- `_denoise_sync()` (line 70-114) — noise reduction pipeline
- `_convert_sync()` (line 134-160) — ffmpeg format conversion

CPU-only, ~5x real-time performance.

### 3c. Deploy + Wire

- Build images, push to `registry.arunlabs.com/`
- Create manifests in Synapse repo:
  - `pyannote-speaker.yaml` — CPU-only, 4Gi/16Gi, HF_TOKEN from secret
  - `deepfilter-audio.yaml` — CPU-only, 2Gi/8Gi
- Add `gateway/src/router_speaker.py` and `gateway/src/router_audio.py`
- Update `config/backends.yaml`

### 3d. Validation

```bash
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@noisy.wav" -o clean.wav

curl -X POST https://synapse.arunlabs.com/speakers/diarize \
  -F "file=@meeting.wav" | jq .

curl -X POST https://synapse.arunlabs.com/speakers/verify \
  -F "file1=@speaker_a.wav" -F "file2=@speaker_b.wav" | jq .
```

---

## Phase 4: Cleanup

### 4a. coqui-tts-server repo

- DELETE old `src/` directory (all engine code migrated to backends/)
- DELETE old monolith `Dockerfile`, `manifests/`, `requirements.txt`
- Keep `backends/` as the canonical source for speech microservices
- Update README.md and CLAUDE.md

### 4b. Synapse repo

- DELETE `manifests/apps/bifrost.yaml` (already replaced in Phase 1)
- DELETE `config/litellm-config.yaml` (cancelled — not needed)
- Update `docs/BUILD-PLAN.md` — Phase 3 (LiteLLM) cancelled, replaced by custom gateway
- Update README.md, CLAUDE.md

### 4c. Kubernetes cleanup

```bash
# Delete Bifrost deployment (if not already done)
sudo kubectl delete deploy bifrost-gateway -n llm-infra
sudo kubectl delete svc bifrost-gateway -n llm-infra
sudo kubectl delete configmap bifrost-config -n llm-infra

# Delete old tts namespace (monolith no longer needed)
sudo kubectl delete ns tts

# Verify all services healthy
curl https://synapse.arunlabs.com/health | jq .
```

---

## API Surface (All Endpoints)

| Method | Path                          | Backend          | Purpose                          |
| ------ | ----------------------------- | ---------------- | -------------------------------- |
| GET    | /health                       | Gateway (local)  | Aggregated backend health        |
| POST   | /v1/chat/completions          | Ollama / vLLM    | LLM chat completion              |
| POST   | /v1/embeddings                | llama-embed      | Text embeddings                  |
| GET    | /v1/models                    | All LLM backends | Available models                 |
| GET    | /voices                       | Gateway (local)  | List voice library               |
| POST   | /voices                       | Gateway (local)  | Upload voice (1-10 WAV files)    |
| POST   | /voices/{voice_id}/references | Gateway (local)  | Add more reference samples       |
| DELETE | /voices/{voice_id}            | Gateway (local)  | Remove a voice                   |
| POST   | /tts/synthesize               | Chatterbox TTS   | Text → WAV with voice cloning    |
| POST   | /tts/stream                   | Chatterbox TTS   | Text → SSE audio stream          |
| POST   | /tts/interpolate              | Chatterbox TTS   | Blend 2-5 voices → WAV           |
| POST   | /stt/transcribe               | Whisper STT      | Audio → transcript + timestamps  |
| POST   | /stt/detect-language          | Whisper STT      | Audio → detected language        |
| POST   | /stt/stream                   | Whisper STT      | Audio → SSE transcript stream    |
| POST   | /speakers/diarize             | pyannote Speaker | Audio → who spoke when           |
| POST   | /speakers/verify              | pyannote Speaker | Two audio files → same/different |
| POST   | /audio/denoise                | DeepFilter Audio | Audio → cleaned WAV              |
| POST   | /audio/convert                | DeepFilter Audio | Audio → different format         |

---

## Risk Mitigations

| Risk                                    | Mitigation                                                                        |
| --------------------------------------- | --------------------------------------------------------------------------------- |
| Custom gateway = maintenance burden     | ~400 lines total, simple HTTP proxy. Less code than configuring Bifrost/LiteLLM.  |
| Chatterbox GPU contention with vLLM     | k8s GPU scheduling. Only one gets GPU at a time. Chatterbox can fall back to CPU. |
| Bifrost removal breaks existing clients | No external clients yet — Bifrost only used internally. Safe to replace.          |
| Chatterbox API mismatch                 | Gateway translates our API ↔ Chatterbox API. Clients see stable interface.        |
| Backend outage                          | Circuit breaker in backend_client.py. Gateway returns 503 with clear error.       |
| Voice PVC in wrong namespace            | Gateway now in llm-infra (not tts). Create new PVC in llm-infra.                  |
| pyannote gated models                   | Requires HF_TOKEN secret + user acceptance of model terms on HuggingFace.         |
