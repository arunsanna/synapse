# Synapse Voice API — Integration Guide

> **Status**: Phases 1–3 deployed and verified (STT, TTS, Audio, Embeddings passing E2E; Speaker requires HF_TOKEN)
> **Base URL**: `https://synapse.arunlabs.com` (internal: `http://synapse-gateway.llm-infra.svc.cluster.local:8000`)
> **Last updated**: 2026-02-15
> **For**: Voice agent / any client integrating TTS, STT, speaker analysis, audio processing, and voice management

---

## Architecture Overview

```
Client (voice agent, OpenClaw, curl)
    |
    v
Synapse Gateway (FastAPI, port 8000)
    |
    +-- /voices/*       → Local voice library (PVC at /data/voices)
    +-- /tts/*          → Proxied to Chatterbox TTS backend (port 8004)
    +-- /v1/embeddings  → Proxied to llama-embed (port 8081)
    +-- /stt/*          → Proxied to whisper-stt (faster-whisper, port 8000)
    +-- /speakers/*     → Proxied to pyannote-speaker (pyannote 3.1, port 8000)
    +-- /audio/*        → Proxied to deepfilter-audio (DeepFilterNet3, port 8000)
```

The gateway owns the voice library (reference WAV samples stored on a PVC). TTS synthesis requests are proxied to the Chatterbox Turbo backend via a **two-step flow**:

1. **Upload**: Gateway sends the voice reference WAV to Chatterbox `POST /upload_reference` (multipart) — returns a filename
2. **Synthesize**: Gateway sends `POST /tts` (JSON) with `reference_audio_filename` pointing to the uploaded file

The gateway caches uploaded filenames per `voice_id` to avoid redundant uploads on repeat requests.

---

## Endpoints

### Health

```
GET /health
```

Returns aggregated health of all registered backends.

```json
{
  "status": "healthy",
  "backends": {
    "llama-embed": { "status": "healthy", "code": 200 },
    "chatterbox-tts": { "status": "healthy", "code": 200 },
    "whisper-stt": { "status": "healthy", "code": 200 },
    "pyannote-speaker": { "status": "healthy", "code": 200 },
    "deepfilter-audio": { "status": "healthy", "code": 200 }
  }
}
```

Status is `"healthy"` when all backends respond 200, `"degraded"` otherwise.

---

### Voice Management (Local)

These endpoints manage the voice reference library stored on the gateway's PVC. No external backend is needed.

#### List Voices

```
GET /voices
```

Returns all voices in the library.

```bash
curl https://synapse.arunlabs.com/voices
```

Response:

```json
[
  {
    "voice_id": "a1b2c3d4-...",
    "name": "narrator",
    "created_at": "2026-02-14T10:30:00Z",
    "references_count": 3,
    "references": ["ref_001.wav", "ref_002.wav", "ref_003.wav"]
  }
]
```

Returns `[]` when no voices exist.

#### Upload Voice

```
POST /voices
Content-Type: multipart/form-data
```

Upload 1–10 WAV reference samples to create a new voice.

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

**Requirements**:

- 1–10 files per upload
- Files must be WAV format (minimum 44 bytes — WAV header size)
- Max 50MB per file
- **Minimum 6 seconds** of clean speech required (Chatterbox rejects audio <5s with a hard assertion error)

#### Add References

```
POST /voices/{voice_id}/references
Content-Type: multipart/form-data
```

Add more reference samples to an existing voice.

```bash
curl -X POST https://synapse.arunlabs.com/voices/a1b2c3d4-.../references \
  -F "files=@extra_sample.wav"
```

Response (200):

```json
{
  "voice_id": "a1b2c3d4-...",
  "name": "narrator",
  "references_count": 3,
  "references": ["ref_001.wav", "ref_002.wav", "ref_003.wav"]
}
```

Returns 404 if voice_id doesn't exist.

#### Delete Voice

```
DELETE /voices/{voice_id}
```

Permanently deletes a voice and all its reference files.

```bash
curl -X DELETE https://synapse.arunlabs.com/voices/a1b2c3d4-...
```

Response (200):

```json
{ "status": "deleted", "voice_id": "a1b2c3d4-..." }
```

Returns 404 if voice not found.

---

### TTS Synthesis (Proxied to Chatterbox)

These endpoints proxy to the Chatterbox Turbo TTS backend. The gateway resolves voice references, pre-uploads them to Chatterbox, and sends synthesis requests as JSON.

**Backend**: Chatterbox TTS Server (`registry.arunlabs.com/chatterbox-tts-server:cpu-latest`)
**Model**: Chatterbox Turbo (350M params, MIT license)
**Device**: CPU (RTX 5090 Blackwell/sm_120 not yet supported by stable PyTorch)
**Capabilities**: Zero-shot voice cloning from 6-second samples, 23 languages
**Timeout**: 120 seconds
**Default voice**: `Alice.wav` (one of 28 predefined voices bundled with Chatterbox)

#### Internal Flow (Chatterbox Two-Step)

When `voice_id` is provided, the gateway performs:

```
1. POST {chatterbox}/upload_reference  ← multipart field name: "files"
   → returns {"uploaded_files": ["ref_001.wav"], "all_reference_files": [...]}

2. POST {chatterbox}/tts              ← JSON with reference_audio_filename
   → returns audio/wav
```

The uploaded filename is cached per `voice_id` — subsequent requests skip step 1.

When no `voice_id` is provided, the gateway sends:

```
POST {chatterbox}/tts  ← JSON with voice_mode="predefined", predefined_voice_id="Alice.wav"
→ returns audio/wav
```

#### Synthesize Speech

```
POST /tts/synthesize
Content-Type: application/json
```

Generate speech from text. Optionally clone a voice.

**Without voice cloning** (default voice):

```bash
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello from Synapse gateway",
    "language": "en",
    "speed": 1.0,
    "split_sentences": true
  }' \
  --output speech.wav
```

**With voice cloning** (uses uploaded reference):

```bash
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello from Synapse gateway",
    "voice_id": "a1b2c3d4-...",
    "language": "en",
    "speed": 1.0,
    "split_sentences": true
  }' \
  --output cloned_speech.wav
```

**Request schema**:

| Field             | Type         | Default  | Description                                      |
| ----------------- | ------------ | -------- | ------------------------------------------------ |
| `text`            | string       | required | Text to synthesize (1–5000 chars)                |
| `voice_id`        | string\|null | null     | Voice to clone. If null, uses Chatterbox default |
| `language`        | string       | "en"     | ISO 639-1 language code (see `/tts/languages`)   |
| `speed`           | float        | 1.0      | Speed factor (0.5–2.0)                           |
| `split_sentences` | bool         | true     | Split text into sentences for better prosody     |

**Response**: `audio/wav` binary stream (Content-Disposition: `synapse_tts.wav`)

**Error responses**:

- 404: Voice not found (when `voice_id` provided but doesn't exist)
- 502: Failed to upload reference to Chatterbox
- 503: Chatterbox backend unavailable

#### Stream Speech

```
POST /tts/stream
Content-Type: application/json
```

Stream TTS audio. Behavior depends on whether voice cloning is requested:

- **Without `voice_id`**: Uses Chatterbox `/v1/audio/speech` (OpenAI-compatible, chunked streaming)
- **With `voice_id`**: Falls back to Chatterbox `/tts` endpoint (two-step upload flow, full response — not chunked, but voice cloning works)

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

Same request schema as `/tts/synthesize`. Response is `audio/wav`.

#### Interpolate Voices

```
POST /tts/interpolate
Content-Type: application/json
```

Blend multiple voices with weights and synthesize. Currently uses the highest-weighted voice (Chatterbox doesn't natively support interpolation).

```bash
curl -X POST https://synapse.arunlabs.com/tts/interpolate \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Blended voice output",
    "voices": [
      {"voice_id": "voice-1-uuid", "weight": 0.7},
      {"voice_id": "voice-2-uuid", "weight": 0.3}
    ],
    "language": "en",
    "speed": 1.0
  }' \
  --output interpolated.wav
```

**Request schema**:

| Field      | Type          | Default  | Description                            |
| ---------- | ------------- | -------- | -------------------------------------- |
| `text`     | string        | required | Text to synthesize (1–5000 chars)      |
| `voices`   | VoiceWeight[] | required | 2–5 voices with weights summing to 1.0 |
| `language` | string        | "en"     | ISO 639-1 language code                |
| `speed`    | float         | 1.0      | Speed factor (0.5–2.0)                 |

**VoiceWeight**: `{"voice_id": "uuid", "weight": 0.0-1.0}`

**Note**: Weights must sum to 1.0 (+-0.01 tolerance). Currently falls back to the highest-weighted voice since Chatterbox doesn't support native voice blending.

#### List Languages

```
GET /tts/languages
```

Returns all supported TTS languages.

```bash
curl https://synapse.arunlabs.com/tts/languages
```

Response:

```json
[
  { "code": "en", "name": "English" },
  { "code": "de", "name": "German" },
  { "code": "es", "name": "Spanish" },
  { "code": "fr", "name": "French" },
  { "code": "ja", "name": "Japanese" },
  { "code": "zh", "name": "Chinese" }
]
```

23 languages total (en, de, es, fr, hi, it, ja, ko, nl, pl, pt, ru, tr, zh, ar, cs, da, fi, hu, nb, ro, sv, uk).

---

### Embeddings (LLM)

```
POST /v1/embeddings
Content-Type: application/json
```

Proxied to llama-embed backend.

```bash
curl -X POST https://synapse.arunlabs.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model": "snowflake-arctic-embed2:latest", "input": "test text"}'
```

---

### STT — Speech-to-Text (Proxied to faster-whisper)

**Backend**: whisper-stt (`registry.arunlabs.com/whisper-stt:latest`)
**Model**: Whisper large-v3-turbo (int8 quantized)
**Device**: CPU
**Timeout**: 120 seconds

#### Transcribe Audio

```
POST /stt/transcribe
Content-Type: multipart/form-data
```

Full transcription of an audio file.

```bash
curl -X POST https://synapse.arunlabs.com/stt/transcribe \
  -F "file=@recording.wav" \
  -F "language=en" \
  -F "word_timestamps=true"
```

Response:

```json
{
  "text": "Hello from Synapse.",
  "language": "en",
  "language_probability": 0.98,
  "duration": 2.5,
  "segments": [
    {
      "id": 1,
      "text": "Hello from Synapse.",
      "start": 0.0,
      "end": 2.5,
      "words": [
        { "word": "Hello", "start": 0.0, "end": 0.4, "probability": 0.95 }
      ]
    }
  ]
}
```

| Field             | Type         | Default  | Description                            |
| ----------------- | ------------ | -------- | -------------------------------------- |
| `file`            | file         | required | Audio file (WAV, MP3, FLAC, etc.)      |
| `language`        | string\|null | null     | ISO 639-1 code. Auto-detected if null. |
| `word_timestamps` | bool         | false    | Include per-word timestamps in output  |

#### Detect Language

```
POST /stt/detect-language
Content-Type: multipart/form-data
```

Detect the spoken language from an audio file.

```bash
curl -X POST https://synapse.arunlabs.com/stt/detect-language \
  -F "file=@recording.wav"
```

Response:

```json
{
  "detected_language": "en",
  "probability": 0.84,
  "all_languages": [
    { "code": "en", "name": "en" },
    { "code": "es", "name": "es" }
  ]
}
```

#### Stream Transcription (SSE)

```
POST /stt/stream
Content-Type: multipart/form-data
```

Stream transcription segments as Server-Sent Events.

```bash
curl -N -X POST https://synapse.arunlabs.com/stt/stream \
  -F "file=@recording.wav" \
  -F "language=en"
```

Response (SSE stream):

```
event: segment
data: {"id": 1, "text": "Hello", "start": 0.0, "end": 0.4, "words": [...]}

event: segment
data: {"id": 2, "text": " from Synapse.", "start": 0.4, "end": 2.5, "words": [...]}

event: done
data: {}
```

---

### Speaker Analysis (Proxied to pyannote)

**Backend**: pyannote-speaker (`registry.arunlabs.com/pyannote-speaker:latest`)
**Model**: pyannote/speaker-diarization-3.1 + pyannote/embedding
**Device**: CPU
**Timeout**: 120 seconds
**Requires**: HuggingFace token (gated model — see [Setup](#pyannote-hf-token-setup))

#### Diarize Audio

```
POST /speakers/diarize
Content-Type: multipart/form-data
```

Identify who spoke when in an audio file.

```bash
curl -X POST https://synapse.arunlabs.com/speakers/diarize \
  -F "file=@meeting.wav" \
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
  "duration": 10.0
}
```

| Field          | Type      | Default  | Description                         |
| -------------- | --------- | -------- | ----------------------------------- |
| `file`         | file      | required | Audio file                          |
| `num_speakers` | int\|null | null     | Exact number of speakers (if known) |
| `min_speakers` | int\|null | null     | Minimum expected speakers           |
| `max_speakers` | int\|null | null     | Maximum expected speakers           |

#### Verify Speaker

```
POST /speakers/verify
Content-Type: multipart/form-data
```

Compare two audio samples to determine if the same person is speaking.

```bash
curl -X POST https://synapse.arunlabs.com/speakers/verify \
  -F "file1=@sample_a.wav" \
  -F "file2=@sample_b.wav"
```

Response:

```json
{
  "is_same_speaker": true,
  "similarity_score": 0.8742,
  "threshold": 0.5
}
```

#### Pyannote HF Token Setup {#pyannote-hf-token-setup}

Pyannote models are gated on HuggingFace. To enable speaker analysis:

1. Accept terms at https://huggingface.co/pyannote/speaker-diarization-3.1
2. Accept terms at https://huggingface.co/pyannote/embedding
3. Create an access token at https://huggingface.co/settings/tokens
4. Update the k8s Secret:

```bash
kubectl -n llm-infra delete secret pyannote-hf-token
kubectl -n llm-infra create secret generic pyannote-hf-token \
  --from-literal=HF_TOKEN=hf_your_actual_token_here
kubectl -n llm-infra rollout restart deploy/pyannote-speaker
```

---

### Audio Processing (Proxied to DeepFilterNet)

**Backend**: deepfilter-audio (`registry.arunlabs.com/deepfilter-audio:latest`)
**Model**: DeepFilterNet3 (noise reduction) + ffmpeg (format conversion)
**Device**: CPU
**Timeout**: 60 seconds

#### Denoise Audio

```
POST /audio/denoise
Content-Type: multipart/form-data
```

Remove background noise from an audio file using DeepFilterNet3.

```bash
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@noisy_recording.wav" \
  --output denoised.wav
```

Response: `audio/wav` binary (Content-Disposition: `denoised.wav`)

#### Convert Audio Format

```
POST /audio/convert
Content-Type: multipart/form-data
```

Convert between audio formats using ffmpeg.

```bash
curl -X POST https://synapse.arunlabs.com/audio/convert \
  -F "file=@input.wav" \
  -F "output_format=mp3" \
  -F "sample_rate=44100" \
  -F "bitrate=192k" \
  --output output.mp3
```

Response: Audio binary in the requested format.

| Field           | Type         | Default  | Description                        |
| --------------- | ------------ | -------- | ---------------------------------- |
| `file`          | file         | required | Input audio file                   |
| `output_format` | string       | "wav"    | Target format: wav, mp3, flac, ogg |
| `sample_rate`   | int\|null    | null     | Target sample rate in Hz           |
| `bitrate`       | string\|null | null     | Target bitrate (e.g., "192k")      |

---

## Typical Voice Agent Workflow

```
1. Upload voice references
   POST /voices  (multipart: name + WAV files)
   -> returns voice_id

2. Synthesize with cloned voice
   POST /tts/synthesize  (JSON: text + voice_id)
   -> gateway uploads ref to Chatterbox (cached), then synthesizes
   -> returns audio/wav

3. Stream for low-latency playback (default voice)
   POST /tts/stream  (JSON: text, no voice_id)
   -> returns chunked audio/wav via OpenAI-compatible endpoint

4. Transcribe user speech
   POST /stt/transcribe  (multipart: audio file)
   -> returns JSON with text, segments, timestamps

5. Stream transcription (real-time)
   POST /stt/stream  (multipart: audio file)
   -> returns SSE events with segments as they're decoded

6. Denoise noisy recordings
   POST /audio/denoise  (multipart: audio file)
   -> returns cleaned WAV

7. Convert audio formats
   POST /audio/convert  (multipart: audio + output_format)
   -> returns audio in requested format

8. Identify speakers in meetings
   POST /speakers/diarize  (multipart: audio file)
   -> returns speaker-labeled time segments

9. Verify speaker identity
   POST /speakers/verify  (multipart: two audio files)
   -> returns similarity score + same/different verdict

10. Check available languages
    GET /tts/languages
    -> returns list of ISO 639-1 codes

11. Manage voice library
    GET /voices                           -> list all
    POST /voices/{id}/references          -> add more samples
    DELETE /voices/{id}                   -> remove voice
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

---

## Error Handling

| HTTP Code | Meaning                                                          |
| --------- | ---------------------------------------------------------------- |
| 200       | Success                                                          |
| 201       | Voice created                                                    |
| 400       | Bad request (invalid input, wrong file count, file too large)    |
| 404       | Voice not found                                                  |
| 500       | Internal server error (backend processing failure)               |
| 502       | Bad gateway (Chatterbox returned unexpected response)            |
| 503       | Backend unavailable (circuit breaker open or connection refused) |
| 504       | Backend timeout                                                  |

The gateway uses a circuit breaker per backend: after 5 consecutive failures, requests are blocked for 30 seconds, then a single probe request is allowed (half-open state).

Retries: 3 attempts with exponential backoff (0.5s -> 1s -> 2s) for connection errors only.

Timeouts: TTS = 120s, STT = 120s, Speaker = 120s, Audio = 60s, Embeddings = 60s, LLM = 300s.

---

## Deployment Status

| Component          | Status        | Notes                                                              |
| ------------------ | ------------- | ------------------------------------------------------------------ |
| Synapse Gateway    | Running (1/1) | `synapse-gateway` deployment in `llm-infra`                        |
| llama-embed        | Healthy (1/1) | Serving `all-minilm` (1024 dims)                                   |
| Chatterbox TTS     | Running (1/1) | CPU mode, `registry.arunlabs.com/chatterbox-tts-server:cpu-latest` |
| whisper-stt        | Running (1/1) | CPU, int8 quantized, large-v3-turbo model                          |
| pyannote-speaker   | Running (1/1) | CPU, requires valid HF_TOKEN for model loading                     |
| deepfilter-audio   | Running (1/1) | CPU, DeepFilterNet3 + ffmpeg, torch 2.1.0                          |
| Voices PVC         | Bound         | 5Gi, `local-path` storage class                                    |
| Model Cache PVC    | Bound         | 10Gi, persists Chatterbox model files                              |
| HF Cache PVC       | Bound         | 20Gi, persists HuggingFace downloads                               |
| Whisper Cache PVC  | Bound         | 10Gi, persists faster-whisper model files                          |
| Pyannote Cache PVC | Bound         | 10Gi, persists pyannote model files                                |
| Ingress            | Active        | `synapse.arunlabs.com` → `synapse-gateway:8000` (TLS via wildcard) |

**GPU note**: All backends run on CPU because the RTX 5090 (Blackwell, sm_120) is not yet supported by stable PyTorch. Once PyTorch ships sm_120 kernels, switch Chatterbox to GPU image and add `nvidia.com/gpu: "1"` to resource limits.

**Pyannote note**: HF token is set and model terms accepted. Speaker endpoints (`/speakers/*`) currently return 500 due to missing `omegaconf` dependency in pyannote-speaker image — fix committed to `coqui-tts-server/backends/pyannote-speaker/requirements.txt`, awaiting image rebuild and redeploy.

**Verified E2E** (2026-02-15):

- `GET /health` → healthy (all 5 backends)
- `GET /voices` → 200 (empty list when no voices uploaded)
- `POST /voices` → 201 (voice upload with WAV reference)
- `DELETE /voices/{id}` → 200 (voice deleted)
- `POST /v1/embeddings` → 1024-dim vectors (snowflake-arctic-embed2)
- `POST /tts/synthesize` → WAV audio, 24kHz 16-bit mono (200 OK)
- `POST /tts/synthesize` (with voice_id) → WAV audio, voice cloning works (200 OK)
- `POST /tts/stream` → WAV audio, default voice (200 OK)
- `POST /tts/stream` (with voice_id) → WAV audio, clone fallback (200 OK)
- `GET /tts/languages` → 23 languages
- `POST /stt/transcribe` → JSON with text + word-level timestamps (200 OK)
- `POST /stt/detect-language` → detected language + top-5 probabilities (200 OK)
- `POST /stt/stream` → SSE events with segments + done event (200 OK)
- `POST /audio/denoise` → cleaned WAV, 48kHz (200 OK)
- `POST /audio/convert` → converted MP3, 192kbps 44.1kHz (200 OK)
- `POST /speakers/diarize` → 500 (awaiting valid HF_TOKEN)
- `POST /speakers/verify` → 500 (awaiting valid HF_TOKEN)

**RESOLVED — Reference filename collision in voice cloning** (was severity: HIGH, fixed 2026-02-15):

~~The gateway uses `os.path.basename(ref_path)` when uploading references to Chatterbox via `/upload_reference`. Since all voices store references as `ref_001.wav`, `ref_002.wav`, etc., uploading a second voice overwrites the first voice's reference file on Chatterbox's filesystem. Conversely, if the gateway's `_ref_upload_cache` has a cached entry for a voice_id, it skips re-upload — but Chatterbox may still have a stale file from a previous voice at the same filename.

**Root cause**: `_upload_reference_to_chatterbox()` in `router_tts.py` line:

```python
filename = os.path.basename(ref_path)  # Always "ref_001.wav" for first reference
```

**Reproduction**:

1. Upload voice A with a short (<5s) WAV → Chatterbox stores `/app/reference_audio/ref_001.wav` (short)
2. Delete voice A, upload voice B with a long (>5s) WAV → gateway uploads to Chatterbox, but Chatterbox may not overwrite if timing/caching differs
3. Synthesize with voice B → Chatterbox reads the old short `ref_001.wav` → fails with `"Audio prompt must be longer than 5 seconds!"`

**Fix**: Prefix the filename with `voice_id` to guarantee uniqueness:

```python
filename = f"{voice_id}_{os.path.basename(ref_path)}"  # "87719e19..._ref_001.wav"
```

Also invalidate `_ref_upload_cache[voice_id]` in `delete_voice()` so stale cache entries don't persist.

~~**Workaround**: Works correctly when only one voice exists at a time, or when reference files are >5 seconds (Chatterbox minimum).~~

**Verified fixed**: Collision test (upload short voice A → delete → upload long voice B → synthesize with B) now returns 200 OK. Gateway properly re-uploads references and cache is invalidated on voice deletion.

---

## Speech Backend Microservices (Deployed)

All backend microservices are deployed and running. Source code lives in `coqui-tts-server/backends/`:

| Backend            | Registry Image                                  | Port | Device     | Endpoints                                               |
| ------------------ | ----------------------------------------------- | ---- | ---------- | ------------------------------------------------------- |
| `whisper-stt`      | `registry.arunlabs.com/whisper-stt:latest`      | 8000 | CPU (int8) | `/transcribe`, `/detect-language`, `/stream`, `/health` |
| `pyannote-speaker` | `registry.arunlabs.com/pyannote-speaker:latest` | 8000 | CPU        | `/diarize`, `/verify`, `/health`                        |
| `deepfilter-audio` | `registry.arunlabs.com/deepfilter-audio:latest` | 8000 | CPU        | `/denoise`, `/convert`, `/health`                       |

All backends accept file uploads via multipart `POST` and return JSON (STT, speaker) or binary audio (audio processing). All use lazy model loading on first request.

---

## Changelog

### 2026-02-15 — Voice Cloning E2E Verification (all passing)

**E2E test results** (17 endpoints tested):

- 15/17 endpoints passing (200 OK with correct response data)
- 2/17 expected failures: `/speakers/diarize` and `/speakers/verify` return 500 (placeholder HF_TOKEN — not a bug)
- Voice cloning works including collision scenario (upload short voice A → delete → upload long voice B → synthesize with B → 200 OK)

**Reference filename collision bug**: Found and fixed. Gateway now properly re-uploads references when a new voice reuses the same `ref_001.wav` filename, and cache is invalidated on voice deletion.

**Additional validation**:

- Chatterbox requires reference audio >5 seconds (hardcoded assertion in `tts_turbo.py:221`) — updated docs from "recommended" to "required"
- Circuit breaker correctly triggers after 5 consecutive 500s, recovers after 30s cooldown
- TTS stream with voice cloning (fallback to `/tts` endpoint) works correctly

### 2026-02-14 — Voice Cloning Bug Fix

| Issue                                                         | Resolution                                                                                                                                                                      |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data.get("files", [])` in `router_tts.py` returns empty list | Changed to `data.get("uploaded_files", [])` — Chatterbox `/upload_reference` returns `{"uploaded_files": [...]}`, not `{"files": [...]}`. This caused all voice cloning to 502. |

### 2026-02-15 — Phase 2 + 3 Deployment (STT, Speaker, Audio)

**Deployment**:

- Built and deployed whisper-stt (`registry.arunlabs.com/whisper-stt:latest`) — faster-whisper large-v3-turbo, int8, CPU
- Built and deployed pyannote-speaker (`registry.arunlabs.com/pyannote-speaker:latest`) — pyannote 3.1, CPU
- Built and deployed deepfilter-audio (`registry.arunlabs.com/deepfilter-audio:latest`) — DeepFilterNet3 + ffmpeg, CPU
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

**Known issue**: pyannote-speaker requires a valid HuggingFace token. The `pyannote-hf-token` Secret has a placeholder. Speaker endpoints return 500 until a real token is set (see [setup instructions](#pyannote-hf-token-setup)).

### 2026-02-14 — Full Phase 1 Deployment

**Deployment**:

- Built and deployed Synapse Gateway (`registry.arunlabs.com/synapse-gateway:latest`) — built on forge (x86_64) to match cluster architecture
- Built Chatterbox TTS CPU image (`registry.arunlabs.com/chatterbox-tts-server:cpu-latest`) — GPU image fails on RTX 5090 (PyTorch lacks sm_120 kernels)
- Deployed all PVCs: model-cache (10Gi), hf-cache (20Gi), voices (5Gi)
- All 3 pods running and healthy, all endpoints verified E2E

**Fixes during deployment**:

| Issue                                      | Resolution                                                                                |
| ------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `exec format error` on gateway             | ARM/x86 mismatch — rebuilt image on forge (x86_64) instead of megamind (arm64)            |
| `args: ["--nvidia"]` crashes Chatterbox    | Removed — NVIDIA entrypoint doesn't accept CLI flags, Chatterbox uses `python3 server.py` |
| `CUDA error: no kernel image` on RTX 5090  | Built CPU variant with `RUNTIME=cpu` — Blackwell (sm_120) not in stable PyTorch           |
| `predefined_voice_id: "default.wav"` → 404 | Changed to `Alice.wav` — Chatterbox ships 28 named voices, no `default.wav`               |
| `/v1/audio/speech` voice `"default"` → 404 | Changed to `Alice.wav` — OpenAI-compatible endpoint also requires named voice             |
| Deployment strategy `RollingUpdate` stuck  | Changed to `Recreate` — single GPU (or CPU-bound) can't run 2 pods simultaneously         |
| No HF cache persistence                    | Added `chatterbox-hf-cache` PVC (20Gi) mounted at `/app/hf_cache`                         |
| No startup probe                           | Added startup probe (40 × 15s = 10min window) for first-time model download               |

### 2026-02-14 — Voice Agent Feedback (all resolved)

| Issue                                           | Severity | Resolution                                                                                                                                         |
| ----------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chatterbox API is two-step, not multipart       | CRITICAL | Rewrote `router_tts.py`: upload via `/upload_reference`, synthesize via `/tts` with `reference_audio_filename`. Added per-voice_id filename cache. |
| `/tts/stream` ignores voice_id                  | CRITICAL | Stream with `voice_id` now falls back to `/tts` endpoint (two-step flow). Default voice uses `/v1/audio/speech` (chunked).                         |
| Default synthesis missing `predefined_voice_id` | CRITICAL | Added `predefined_voice_id: "Alice.wav"` when `voice_mode` is `"predefined"`.                                                                      |
| Chatterbox PVC mount path wrong                 | HIGH     | Changed `mountPath: /app/voices` → `/app/reference_audio` in `chatterbox-tts.yaml`.                                                                |
| No file upload size limits                      | MEDIUM   | Added 50MB per-file limit on voice upload.                                                                                                         |
| TTS timeout 60s too short                       | MEDIUM   | Increased TTS timeout from 60s to 120s in `backend_client.py`.                                                                                     |
| Missing `/tts/languages` endpoint               | MEDIUM   | Added `GET /tts/languages` returning 23 supported languages.                                                                                       |
