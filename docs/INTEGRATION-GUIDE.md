# Synapse Integration Guide

Practical workflows for integrating with the Synapse AI gateway. This guide covers common tasks end-to-end with working examples.

**Base URL**: `https://synapse.arunlabs.com`
**Internal URL**: `http://synapse-gateway.llm-infra.svc.cluster.local:8000`

For complete endpoint specifications, request/response schemas, and error codes, see the [API Reference](API.md).

---

## Workflow 1: Voice Cloning (End-to-End)

Synapse uses Chatterbox Turbo for zero-shot voice cloning. The flow is: upload voice reference samples, then synthesize speech using those samples as the target voice.

### Step 1: Upload Voice References

Create a voice profile by uploading one or more WAV reference samples. Each sample must contain at least 6 seconds of clean speech (Chatterbox rejects audio under 5 seconds with a hard assertion error).

```bash
curl -X POST https://synapse.arunlabs.com/voices \
  -F "name=narrator" \
  -F "files=@sample1.wav" \
  -F "files=@sample2.wav"
```

Response (201):

```json
{
  "voice_id": "a1b2c3d4-...",
  "name": "narrator",
  "references_count": 2,
  "references": ["ref_001.wav", "ref_002.wav"]
}
```

Save the `voice_id` -- you will use it in all subsequent synthesis requests.

**Requirements**:

- WAV format, minimum 44 bytes (WAV header size)
- 1-10 files per upload, max 50MB per file
- Minimum 6 seconds of clean speech per sample
- More samples generally improve voice quality

### Step 2: Synthesize with Cloned Voice

Pass the `voice_id` to generate speech in the cloned voice:

```bash
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello from Synapse gateway",
    "voice_id": "a1b2c3d4-...",
    "language": "en",
    "speed": 1.0
  }' \
  --output cloned_speech.wav
```

The response is a `audio/wav` binary stream (24kHz, 16-bit mono).

**What happens internally**: The gateway uses a two-step flow with Chatterbox:

1. Uploads the voice reference WAV to Chatterbox via `POST /upload_reference`
2. Sends `POST /tts` with the uploaded filename to synthesize

The gateway caches the uploaded filename per `voice_id`, so repeat requests skip the upload step.

### Step 3: Stream for Low-Latency Playback

For lower-latency playback, use the streaming endpoint. Without a `voice_id`, this uses Chatterbox's OpenAI-compatible chunked streaming endpoint:

```bash
curl -X POST https://synapse.arunlabs.com/tts/stream \
  -H "Content-Type: application/json" \
  -d '{
    "text": "This is a streaming test",
    "language": "en",
    "speed": 1.0
  }' \
  --output streamed.wav
```

With a `voice_id`, the stream endpoint falls back to the two-step flow (full response, not chunked) because Chatterbox's streaming endpoint does not support custom voice references.

### Step 4: Manage Your Voice Library

**List all voices:**

```bash
curl https://synapse.arunlabs.com/voices
```

**Add more reference samples to an existing voice:**

```bash
curl -X POST https://synapse.arunlabs.com/voices/a1b2c3d4-.../references \
  -F "files=@extra_sample.wav"
```

**Delete a voice:**

```bash
curl -X DELETE https://synapse.arunlabs.com/voices/a1b2c3d4-...
```

### Synthesize Without Voice Cloning

If you do not need voice cloning, omit the `voice_id` field. The gateway uses the `Alice.wav` predefined voice (one of 28 voices bundled with Chatterbox):

```bash
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello from Synapse", "language": "en"}' \
  --output speech.wav
```

### Check Available Languages

Chatterbox supports 23 languages:

```bash
curl https://synapse.arunlabs.com/tts/languages
```

Returns: en, de, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, zh, ar, cs, da, fi, hu, nb, ro, sv, uk.

---

## Workflow 2: Meeting Transcription Pipeline

Combine STT, speaker diarization, and audio denoising to produce a full meeting transcript with speaker labels.

### Step 1: Denoise the Recording

Clean up background noise before transcription for better accuracy:

```bash
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@meeting_recording.wav" \
  --output meeting_clean.wav
```

### Step 2: Transcribe with Word Timestamps

Transcribe the cleaned audio with word-level timestamps enabled:

```bash
curl -X POST https://synapse.arunlabs.com/stt/transcribe \
  -F "file=@meeting_clean.wav" \
  -F "language=en" \
  -F "word_timestamps=true"
```

Response:

```json
{
  "text": "Welcome everyone. Let's begin the meeting.",
  "language": "en",
  "language_probability": 0.98,
  "duration": 45.2,
  "segments": [
    {
      "id": 1,
      "text": "Welcome everyone.",
      "start": 0.0,
      "end": 1.8,
      "words": [
        { "word": "Welcome", "start": 0.0, "end": 0.6, "probability": 0.95 },
        { "word": "everyone.", "start": 0.7, "end": 1.8, "probability": 0.92 }
      ]
    }
  ]
}
```

### Step 3: Identify Speakers

Run speaker diarization on the same audio to find who spoke when:

```bash
curl -X POST https://synapse.arunlabs.com/speakers/diarize \
  -F "file=@meeting_clean.wav" \
  -F "min_speakers=2" \
  -F "max_speakers=5"
```

Response:

```json
{
  "num_speakers": 3,
  "segments": [
    { "speaker": "SPEAKER_00", "start": 0.5, "end": 3.2 },
    { "speaker": "SPEAKER_01", "start": 3.5, "end": 7.1 },
    { "speaker": "SPEAKER_00", "start": 7.3, "end": 10.0 }
  ],
  "duration": 45.2
}
```

### Step 4: Combine Transcription with Speaker Labels

Map speaker segments to transcription text by overlapping timestamps. Here is a complete Python example:

```python
import requests

BASE = "https://synapse.arunlabs.com"

# Step 1: Denoise
with open("meeting_recording.wav", "rb") as f:
    resp = requests.post(f"{BASE}/audio/denoise", files={"file": f})
    with open("meeting_clean.wav", "wb") as out:
        out.write(resp.content)

# Step 2: Transcribe with word timestamps
with open("meeting_clean.wav", "rb") as f:
    transcript = requests.post(
        f"{BASE}/stt/transcribe",
        files={"file": f},
        data={"language": "en", "word_timestamps": "true"},
    ).json()

# Step 3: Diarize
with open("meeting_clean.wav", "rb") as f:
    diarization = requests.post(
        f"{BASE}/speakers/diarize",
        files={"file": f},
        data={"min_speakers": "2", "max_speakers": "5"},
    ).json()

# Step 4: Merge -- assign speaker labels to transcript segments
def find_speaker(time_point, speaker_segments):
    """Find the speaker active at a given time point."""
    for seg in speaker_segments:
        if seg["start"] <= time_point <= seg["end"]:
            return seg["speaker"]
    return "UNKNOWN"

speaker_segments = diarization["segments"]

labeled_transcript = []
for segment in transcript["segments"]:
    midpoint = (segment["start"] + segment["end"]) / 2
    speaker = find_speaker(midpoint, speaker_segments)
    labeled_transcript.append({
        "speaker": speaker,
        "start": segment["start"],
        "end": segment["end"],
        "text": segment["text"],
    })

# Print the labeled transcript
current_speaker = None
for entry in labeled_transcript:
    if entry["speaker"] != current_speaker:
        current_speaker = entry["speaker"]
        print(f"\n[{current_speaker}]")
    print(f"  {entry['text']}")
```

Output:

```
[SPEAKER_00]
  Welcome everyone. Let's begin the meeting.

[SPEAKER_01]
  Thanks for joining. I have three items on the agenda.

[SPEAKER_00]
  Sounds good, let's start with the first one.
```

### Alternative: Stream Transcription in Real-Time

For real-time UX, use the SSE streaming endpoint instead of batch transcription:

```bash
curl -N -X POST https://synapse.arunlabs.com/stt/stream \
  -F "file=@meeting_clean.wav" \
  -F "language=en"
```

Returns Server-Sent Events as segments are decoded:

```
event: segment
data: {"id": 1, "text": "Welcome everyone.", "start": 0.0, "end": 1.8}

event: segment
data: {"id": 2, "text": "Let's begin the meeting.", "start": 1.9, "end": 3.5}

event: done
data: {}
```

---

## Workflow 3: Speaker Verification

Determine whether two audio samples are from the same person. Useful for voice-based authentication or identity confirmation.

### Step 1: Collect Two Audio Samples

You need two WAV files: one known reference and one to verify against.

### Step 2: Verify Identity

```bash
curl -X POST https://synapse.arunlabs.com/speakers/verify \
  -F "file1=@known_speaker.wav" \
  -F "file2=@unknown_speaker.wav"
```

Response:

```json
{
  "is_same_speaker": true,
  "similarity_score": 0.8742,
  "threshold": 0.5
}
```

### Step 3: Interpret the Result

- `similarity_score` ranges from 0.0 (different speakers) to 1.0 (identical)
- `threshold` is the decision boundary (default: 0.5)
- `is_same_speaker` is `true` when `similarity_score >= threshold`
- Scores above 0.8 indicate high confidence of same speaker
- Scores below 0.3 indicate high confidence of different speakers
- Scores between 0.3 and 0.6 are ambiguous -- consider collecting better samples

---

## Workflow 4: Audio Processing

### Denoise a Recording

Remove background noise using DeepFilterNet3:

```bash
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@noisy_recording.wav" \
  --output denoised.wav
```

Returns a cleaned `audio/wav` file (48kHz).

### Convert Audio Format

Convert between WAV, MP3, FLAC, and OGG using ffmpeg:

```bash
curl -X POST https://synapse.arunlabs.com/audio/convert \
  -F "file=@input.wav" \
  -F "output_format=mp3" \
  -F "sample_rate=44100" \
  -F "bitrate=192k" \
  --output output.mp3
```

### Chain Operations

A common pattern is to denoise first, then convert:

```bash
# Denoise
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@noisy_recording.wav" \
  --output clean.wav

# Convert the cleaned file to MP3
curl -X POST https://synapse.arunlabs.com/audio/convert \
  -F "file=@clean.wav" \
  -F "output_format=mp3" \
  -F "bitrate=192k" \
  --output clean.mp3
```

---

## Voice Storage Layout

Voices are stored on a Kubernetes PVC mounted at `/data/voices` (gateway) and `/app/reference_audio` (Chatterbox):

```
/data/voices/                          (gateway PVC)
+-- {voice_id}/
    +-- metadata.json                  # name, created_at, duration
    +-- references/
        +-- ref_001.wav
        +-- ref_002.wav
        +-- ...

/app/reference_audio/                  (Chatterbox PVC, shared)
+-- ref_001.wav                        # uploaded by gateway via /upload_reference
+-- ...
```

Legacy single-reference format (`reference.wav` at voice root) is auto-migrated to the `references/` directory on first access.

---

## Configuration

The gateway reads `config/backends.yaml` (mounted as ConfigMap):

```yaml
backends:
  llama-embed:
    url: http://llama-embed.llm-infra.svc.cluster.local:8081
    type: openai-compatible
    health: /health
  chatterbox-tts:
    url: http://chatterbox-tts.llm-infra.svc.cluster.local:8004
    type: chatterbox
    health: /api/ui/initial-data
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

routes:
  /v1/embeddings: llama-embed
  /tts/*: chatterbox-tts
  /stt/*: whisper-stt
  /speakers/*: pyannote-speaker
  /audio/*: deepfilter-audio
```

Environment variables (prefix `SYNAPSE_`):

| Variable                      | Default                 | Description              |
| ----------------------------- | ----------------------- | ------------------------ |
| `SYNAPSE_GATEWAY_CONFIG_PATH` | `/config/backends.yaml` | Path to backend registry |
| `SYNAPSE_VOICE_LIBRARY_DIR`   | `/data/voices`          | Voice reference storage  |
| `SYNAPSE_LOG_LEVEL`           | `INFO`                  | Logging level            |

For full backend configuration details, see the [API Reference](API.md).

---

## Deployment Status

| Component          | Status        | Notes                                                              |
| ------------------ | ------------- | ------------------------------------------------------------------ |
| Synapse Gateway    | Running (1/1) | `synapse-gateway` deployment in `llm-infra`                        |
| llama-embed        | Healthy (1/1) | Serving `all-minilm` (1024 dims)                                   |
| Chatterbox TTS     | Running (1/1) | CPU mode, `registry.arunlabs.com/chatterbox-tts-server:cpu-latest` |
| whisper-stt        | Running (1/1) | CPU, int8 quantized, large-v3-turbo model                          |
| pyannote-speaker   | Running (1/1) | CPU, HF_TOKEN configured, models loaded                            |
| deepfilter-audio   | Running (1/1) | CPU, DeepFilterNet3 + ffmpeg, torch 2.1.0                          |
| Voices PVC         | Bound         | 5Gi, `local-path` storage class                                    |
| Model Cache PVC    | Bound         | 10Gi, persists Chatterbox model files                              |
| HF Cache PVC       | Bound         | 20Gi, persists HuggingFace downloads                               |
| Whisper Cache PVC  | Bound         | 10Gi, persists faster-whisper model files                          |
| Pyannote Cache PVC | Bound         | 10Gi, persists pyannote model files                                |
| Ingress            | Active        | `synapse.arunlabs.com` via Traefik (TLS via wildcard)              |

**GPU note**: All backends run on CPU because the RTX 5090 (Blackwell, sm_120) is not yet supported by stable PyTorch. Once PyTorch ships sm_120 kernels, switch Chatterbox to GPU image and add `nvidia.com/gpu: "1"` to resource limits.

**Verified E2E** (2026-02-15): 17/17 endpoints passing.

---

## Changelog

### 2026-02-15 -- Voice Cloning E2E Verification (all passing)

**E2E test results** (17 endpoints tested):

- 17/17 endpoints passing (200 OK with correct response data)
- Voice cloning works including collision scenario (upload short voice A -> delete -> upload long voice B -> synthesize with B -> 200 OK)

**Reference filename collision bug**: Found and fixed. Gateway now prefixes uploaded filenames with `voice_id` to prevent collisions when multiple voices use the same `ref_001.wav` filename. Cache is invalidated on voice deletion.

**Additional validation**:

- Chatterbox requires reference audio >5 seconds (hardcoded assertion in `tts_turbo.py:221`)
- Circuit breaker correctly triggers after 5 consecutive 500s, recovers after 30s cooldown
- TTS stream with voice cloning (fallback to `/tts` endpoint) works correctly

### 2026-02-15 -- Phase 2 + 3 Deployment (STT, Speaker, Audio)

**Deployment**:

- Built and deployed whisper-stt (faster-whisper large-v3-turbo, int8, CPU)
- Built and deployed pyannote-speaker (pyannote 3.1, CPU)
- Built and deployed deepfilter-audio (DeepFilterNet3 + ffmpeg, CPU)
- Replaced all 501 stub routers with real multipart-file-forwarding proxies in gateway
- Updated gateway ConfigMap with 3 new backend URLs and routes
- All 6 pods running (gateway + 5 backends), all health checks passing

**Fixes during deployment**:

| Issue                                       | Resolution                                                                  |
| ------------------------------------------- | --------------------------------------------------------------------------- |
| `groupadd: group 'audio' already exists`    | Renamed user/group from `audio` to `appuser` in deepfilter-audio Dockerfile |
| `No module named 'torchaudio'`              | Added `torchaudio` to deepfilter-audio requirements.txt                     |
| `No module named 'torchaudio.backend'`      | Pinned `torch==2.1.0` + `torchaudio==2.1.0` (DeepFilterNet needs older API) |
| `No such file or directory: 'git'`          | Added `git` to deepfilter-audio Dockerfile apt-get install                  |
| `use_auth_token` TypeError in pyannote      | Changed `use_auth_token=` to `token=` in pyannote-speaker main.py           |
| `imagePullPolicy: IfNotPresent` stale cache | Temporarily patched to `Always` to force pull new images, then reverted     |

**Resolved**: pyannote-speaker HF_TOKEN configured and speaker endpoints fully operational.

### 2026-02-14 -- Voice Cloning Bug Fix

| Issue                                                         | Resolution                                                                                                                                                                       |
| ------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data.get("files", [])` in `router_tts.py` returns empty list | Changed to `data.get("uploaded_files", [])` -- Chatterbox `/upload_reference` returns `{"uploaded_files": [...]}`, not `{"files": [...]}`. This caused all voice cloning to 502. |

### 2026-02-14 -- Full Phase 1 Deployment

**Deployment**:

- Built and deployed Synapse Gateway (`registry.arunlabs.com/synapse-gateway:latest`)
- Built Chatterbox TTS CPU image (`registry.arunlabs.com/chatterbox-tts-server:cpu-latest`)
- Deployed all PVCs: model-cache (10Gi), hf-cache (20Gi), voices (5Gi)
- All 3 pods running and healthy, all endpoints verified E2E

**Fixes during deployment**:

| Issue                                       | Resolution                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `exec format error` on gateway              | ARM/x86 mismatch -- rebuilt image on forge (x86_64) instead of megamind (arm64)            |
| `args: ["--nvidia"]` crashes Chatterbox     | Removed -- NVIDIA entrypoint doesn't accept CLI flags, Chatterbox uses `python3 server.py` |
| `CUDA error: no kernel image` on RTX 5090   | Built CPU variant with `RUNTIME=cpu` -- Blackwell (sm_120) not in stable PyTorch           |
| `predefined_voice_id: "default.wav"` -> 404 | Changed to `Alice.wav` -- Chatterbox ships 28 named voices, no `default.wav`               |
| `/v1/audio/speech` voice `"default"` -> 404 | Changed to `Alice.wav` -- OpenAI-compatible endpoint also requires named voice             |
| Deployment strategy `RollingUpdate` stuck   | Changed to `Recreate` -- single GPU (or CPU-bound) can't run 2 pods simultaneously         |
| No HF cache persistence                     | Added `chatterbox-hf-cache` PVC (20Gi) mounted at `/app/hf_cache`                          |
| No startup probe                            | Added startup probe (40 x 15s = 10min window) for first-time model download                |

### 2026-02-14 -- Voice Agent Feedback (all resolved)

| Issue                                           | Severity | Resolution                                                                                                                                         |
| ----------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chatterbox API is two-step, not multipart       | CRITICAL | Rewrote `router_tts.py`: upload via `/upload_reference`, synthesize via `/tts` with `reference_audio_filename`. Added per-voice_id filename cache. |
| `/tts/stream` ignores voice_id                  | CRITICAL | Stream with `voice_id` now falls back to `/tts` endpoint (two-step flow). Default voice uses `/v1/audio/speech` (chunked).                         |
| Default synthesis missing `predefined_voice_id` | CRITICAL | Added `predefined_voice_id: "Alice.wav"` when `voice_mode` is `"predefined"`.                                                                      |
| Chatterbox PVC mount path wrong                 | HIGH     | Changed `mountPath: /app/voices` to `/app/reference_audio` in `chatterbox-tts.yaml`.                                                               |
| No file upload size limits                      | MEDIUM   | Added 50MB per-file limit on voice upload.                                                                                                         |
| TTS timeout 60s too short                       | MEDIUM   | Increased TTS timeout from 60s to 120s in `backend_client.py`.                                                                                     |
| Missing `/tts/languages` endpoint               | MEDIUM   | Added `GET /tts/languages` returning 23 supported languages.                                                                                       |
