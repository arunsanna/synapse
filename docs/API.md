# Synapse API Reference

Unified gateway for LLM, voice, speech, and audio services.

|                    |                                                           |
| ------------------ | --------------------------------------------------------- |
| **Base URL**       | `https://synapse.arunlabs.com`                            |
| **Internal URL**   | `http://synapse-gateway.llm-infra.svc.cluster.local:8000` |
| **Authentication** | None (cluster/internal trust boundary)                    |
| **OpenAPI spec**   | `GET /docs`, `GET /openapi.json`                          |
| **API endpoints**  | 22 total across 7 service groups                          |

## Table of Contents

- [Health](#health)
- [LLM Routes](#llm-routes)
- [Voice Management](#voice-management)
- [Text-to-Speech (TTS)](#text-to-speech-tts)
- [Speech-to-Text (STT)](#speech-to-text-stt)
- [Speaker Analysis](#speaker-analysis)
- [Audio Processing](#audio-processing)
- [Error Reference](#error-reference)
- [Circuit Breaker](#circuit-breaker)
- [Timeouts](#timeouts)
- [Configuration](#configuration)

## Endpoint Map

| Method   | Path                      | Backend          |
| -------- | ------------------------- | ---------------- |
| `GET`    | `/health`                 | Gateway          |
| `POST`   | `/v1/embeddings`          | llama-embed      |
| `POST`   | `/v1/chat/completions`    | llama-router     |
| `GET`    | `/models`                 | llama-router     |
| `GET`    | `/models/{model_id}/schema` | Gateway local  |
| `GET`    | `/models/{model_id}/profile` | Gateway local |
| `PUT`    | `/models/{model_id}/profile` | Gateway local |
| `POST`   | `/models/{model_id}/profile/apply` | Gateway local |
| `POST`   | `/models/load`            | llama-router     |
| `POST`   | `/models/unload`          | llama-router     |
| `GET`    | `/v1/models`              | Gateway aggregate |
| `GET`    | `/voices`                 | Gateway local    |
| `POST`   | `/voices`                 | Gateway local    |
| `POST`   | `/voices/{voice_id}/references` | Gateway local |
| `DELETE` | `/voices/{voice_id}`      | Gateway local    |
| `POST`   | `/tts/synthesize`         | chatterbox-tts   |
| `POST`   | `/tts/stream`             | chatterbox-tts   |
| `POST`   | `/tts/interpolate`        | chatterbox-tts   |
| `GET`    | `/tts/languages`          | Gateway static   |
| `POST`   | `/stt/transcribe`         | whisper-stt      |
| `POST`   | `/stt/detect-language`    | whisper-stt      |
| `POST`   | `/stt/stream`             | whisper-stt      |
| `POST`   | `/speakers/diarize`       | pyannote-speaker |
| `POST`   | `/speakers/verify`        | pyannote-speaker |
| `POST`   | `/audio/denoise`          | deepfilter-audio |
| `POST`   | `/audio/convert`          | deepfilter-audio |

Non-OpenAPI UI routes: `GET /`, `GET /ui`, `GET /dashboard`.

## Health

### GET /health

Aggregated health for configured backends.

```json
{
  "status": "healthy",
  "backends": {
    "llama-embed": { "status": "healthy", "code": 200 },
    "llama-router": { "status": "healthy", "code": 200 },
    "chatterbox-tts": { "status": "healthy", "code": 200 },
    "whisper-stt": { "status": "healthy", "code": 200 },
    "pyannote-speaker": { "status": "healthy", "code": 200 },
    "deepfilter-audio": { "status": "healthy", "code": 200 }
  }
}
```

Top-level status is `healthy` only when all backends report HTTP 200.

## LLM Routes

### POST /v1/embeddings

OpenAI-compatible embeddings endpoint proxied to `llama-embed`.

Request body:

```json
{
  "model": "snowflake-arctic-embed2:latest",
  "input": "text to embed"
}
```

### POST /v1/chat/completions

OpenAI-compatible chat endpoint proxied to `llama-router`.

Request body:

```json
{
  "model": "auto",
  "messages": [
    { "role": "user", "content": "Explain retries in one paragraph" }
  ],
  "stream": false
}
```

Model behavior:

- Explicit `model`: gateway forwards as-is.
- `model` omitted or set to `auto` aliases: gateway applies lightweight routing policy (general vs coder model), ensures selected model is loaded, and unloads other loaded model when needed.

### GET /models

Returns model status from llama-router (`loaded`, `loading`, `unloaded`, or failure state).

### POST /models/load

Load a router model and optionally store Synapse-side default generation settings.

```json
{ "model": "Qwen3-8B-Q4_K_M" }
```

Optional fields (stored per model in persisted profile storage):

```json
{
  "model": "MiniMax-M2.5-UD-TQ1_0",
  "temperature": 1.0,
  "top_p": 0.95,
  "top_k": 40,
  "system_prompt": "You are a helpful assistant. Your name is MiniMax-M2.5 and is built by MiniMax."
}
```

Behavior:

- `llama-router` still receives only `{ "model": ... }`.
- The optional fields above are applied by Synapse to future `/v1/chat/completions` requests for that model only when the request does not already provide those values.
- `GET /models` exposes configured values under `status.synapse_defaults`.

### GET /models/{model_id}/schema

Returns editable generation parameter schema (types, ranges, descriptions) for a model.

### GET /models/{model_id}/profile

Returns persisted generation profile values for the model.

### PUT /models/{model_id}/profile

Upserts persisted profile values.

```json
{
  "values": {
    "temperature": 1.0,
    "top_p": 0.95,
    "reasoning_effort": "high"
  }
}
```

Set a value to `null` to unset it. Pass `"replace": true` to replace the whole profile.

### POST /models/{model_id}/profile/apply

Apply persisted profile and optionally trigger model load.

```json
{ "load_model": true }
```

### POST /models/unload

Pass-through model unload request to llama-router.

```json
{ "model": "Qwen3-8B-Q4_K_M" }
```

### GET /v1/models

Aggregates model listings from configured LLM backends (`llama-embed`, `llama-router`, optional `vllm`).

## Voice Management

Voice profiles are stored locally on the gateway PVC.

### GET /voices

List all voice profiles.

### POST /voices

Create a voice profile from WAV references.

Form fields:

| Field   | Required | Notes |
| ------- | -------- | ----- |
| `name`  | yes      | Display name |
| `files` | yes      | 1-10 WAV files, max 50MB each |

Response status: `201 Created`.

### POST /voices/{voice_id}/references

Add WAV reference files to an existing voice profile.

Form fields:

| Field   | Required | Notes |
| ------- | -------- | ----- |
| `files` | yes      | 1-10 WAV files, max 50MB each |

### DELETE /voices/{voice_id}

Delete voice profile and stored references.

## Text-to-Speech (TTS)

### POST /tts/synthesize

Synthesize speech via Chatterbox.

Request body:

```json
{
  "text": "Hello from Synapse",
  "voice_id": "optional-voice-id",
  "language": "en",
  "speed": 1.0,
  "split_sentences": true
}
```

Behavior:

- With `voice_id`: gateway uploads reference WAV to Chatterbox (`/upload_reference`) and synthesizes with clone mode.
- Without `voice_id`: gateway uses predefined voice mode (`Alice.wav`).

### POST /tts/stream

Stream TTS audio.

- Default voice: chunked streaming via Chatterbox OpenAI-compatible endpoint.
- With `voice_id`: falls back to non-chunked clone flow because upstream stream API does not support custom reference files.

### POST /tts/interpolate

Accepts 2-5 weighted voices and uses the highest weight voice as clone reference.

Request body:

```json
{
  "text": "Interpolation sample",
  "voices": [
    { "voice_id": "voice-a", "weight": 0.7 },
    { "voice_id": "voice-b", "weight": 0.3 }
  ],
  "language": "en",
  "speed": 1.0
}
```

Weights must sum to `1.0` (+/- 0.01).

### GET /tts/languages

Returns supported Chatterbox language list.

## Speech-to-Text (STT)

### POST /stt/transcribe

Form fields:

| Field             | Required | Notes |
| ----------------- | -------- | ----- |
| `file`            | yes      | Audio file |
| `language`        | no       | Language hint |
| `word_timestamps` | no       | `true` for word-level timing |

### POST /stt/detect-language

Form fields:

| Field  | Required | Notes |
| ------ | -------- | ----- |
| `file` | yes      | Audio file |

### POST /stt/stream

Streams SSE transcription events.

Form fields:

| Field      | Required | Notes |
| ---------- | -------- | ----- |
| `file`     | yes      | Audio file |
| `language` | no       | Language hint |

## Speaker Analysis

### POST /speakers/diarize

Form fields:

| Field          | Required | Notes |
| -------------- | -------- | ----- |
| `file`         | yes      | Audio file |
| `num_speakers` | no       | Fixed speaker count |
| `min_speakers` | no       | Lower bound |
| `max_speakers` | no       | Upper bound |

### POST /speakers/verify

Form fields:

| Field   | Required | Notes |
| ------- | -------- | ----- |
| `file1` | yes      | First voice sample |
| `file2` | yes      | Second voice sample |

## Audio Processing

### POST /audio/denoise

Form fields:

| Field  | Required | Notes |
| ------ | -------- | ----- |
| `file` | yes      | Input audio |

Returns binary `audio/wav`.

### POST /audio/convert

Form fields:

| Field           | Required | Notes |
| --------------- | -------- | ----- |
| `file`          | yes      | Input audio |
| `output_format` | no       | `wav`, `mp3`, `flac`, `ogg` |
| `sample_rate`   | no       | Integer Hz |
| `bitrate`       | no       | Codec bitrate |

Returns converted audio bytes.

## Error Reference

| Code | Meaning | Typical cause |
| ---- | ------- | ------------- |
| 200  | Success | Request processed |
| 201  | Created | Voice profile created |
| 400  | Bad Request | Invalid form/body input |
| 404  | Not Found | Voice ID does not exist |
| 422  | Validation Error | FastAPI schema validation failed |
| 500  | Internal Error | Unexpected gateway exception |
| 502  | Bad Gateway | Upstream backend error envelope |
| 503  | Unavailable | Backend unreachable or circuit open |
| 504  | Timeout | Upstream request exceeded timeout |

## Circuit Breaker

Per backend, gateway uses:

- Threshold: 5 consecutive connection failures
- Cooldown: 30 seconds
- Recovery: half-open probe request
- Retry strategy: 3 attempts with backoff (0.5s, 1s, 2s)

Retry and breaker signals are based on connection-level errors only (`ConnectError`, `ConnectTimeout`).

## Timeouts

| Backend type | Timeout | Routes |
| ------------ | ------- | ------ |
| `llm` | 300s | `/v1/chat/completions`, `/models/load`, `/models/unload` |
| `tts` | 120s | `/tts/*` |
| `stt` | 120s | `/stt/*` |
| `speaker` | 120s | `/speakers/*` |
| `audio` | 60s | `/audio/*` |
| `embeddings` | 60s | `/v1/embeddings` |
| `default` | 60s | health and non-specialized calls |

## Configuration

Gateway config file: `config/backends.yaml`

```yaml
backends:
  llama-embed:
    url: http://llama-embed.llm-infra.svc.cluster.local:8081
    type: openai-compatible
    health: /health

  llama-router:
    url: http://llama-router.llm-infra.svc.cluster.local:8082
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
  /v1/chat/completions: llama-router
  /tts/*: chatterbox-tts
  /stt/*: whisper-stt
  /speakers/*: pyannote-speaker
  /audio/*: deepfilter-audio
```

Environment variables:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `SYNAPSE_GATEWAY_CONFIG_PATH` | `/config/backends.yaml` | Backend registry path |
| `SYNAPSE_VOICE_LIBRARY_DIR` | `/data/voices` | Voice library storage path |
| `SYNAPSE_MODEL_PROFILES_PATH` | `/data/voices/model-profiles.json` | Per-model generation profile storage path |
| `SYNAPSE_LOG_LEVEL` | `INFO` | Gateway log level |
