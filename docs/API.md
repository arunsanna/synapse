# Synapse API Reference

Unified AI gateway for LLM, speech, and audio services.

|                    |                                                             |
| ------------------ | ----------------------------------------------------------- |
| **Base URL**       | `https://synapse.arunlabs.com`                              |
| **Internal URL**   | `http://synapse-gateway.llm-infra.svc.cluster.local:8000`   |
| **Authentication** | None (internal cluster service)                             |
| **OpenAPI spec**   | `GET /docs` (Swagger UI) / `GET /openapi.json` (raw schema) |
| **Endpoints**      | 17 total across 7 service groups                            |

---

## Table of Contents

- [Health](#health)
- [Voice Management](#voice-management)
- [Text-to-Speech (TTS)](#text-to-speech-tts)
- [Speech-to-Text (STT)](#speech-to-text-stt)
- [Speaker Analysis](#speaker-analysis)
- [Audio Processing](#audio-processing)
- [Embeddings](#embeddings)
- [Error Reference](#error-reference)
- [Circuit Breaker](#circuit-breaker)
- [Timeouts](#timeouts)
- [Configuration](#configuration)

---

## API Versioning

- `/v1/embeddings` uses the `/v1/` prefix because it follows the OpenAI-compatible API convention.
- All other endpoints (TTS, STT, speakers, audio) are custom and unversioned.
- The gateway does not currently enforce API versioning.

---

## Health

### GET /health

Returns aggregated health status of all registered backends.

**Response**

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

Top-level `status` is `"healthy"` when all backends return 200, `"degraded"` otherwise.

**Status Codes**

| Code | Description                                                     |
| ---- | --------------------------------------------------------------- |
| 200  | Health check completed (check `status` field for overall state) |

**Examples**

```bash
curl https://synapse.arunlabs.com/health
```

```python
import requests

resp = requests.get("https://synapse.arunlabs.com/health")
data = resp.json()
print(data["status"])  # "healthy" or "degraded"
```

```typescript
const resp = await fetch("https://synapse.arunlabs.com/health");
const data = await resp.json();
console.log(data.status); // "healthy" or "degraded"
```

---

## Voice Management

Manage voice reference samples stored on the gateway's local PVC. These endpoints do not proxy to any backend.

### GET /voices

List all voices in the library.

**Response**

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

**Status Codes**

| Code | Description         |
| ---- | ------------------- |
| 200  | Voice list returned |

**Examples**

```bash
curl https://synapse.arunlabs.com/voices
```

```python
import requests

resp = requests.get("https://synapse.arunlabs.com/voices")
voices = resp.json()
for v in voices:
    print(f"{v['name']} ({v['references_count']} refs)")
```

```typescript
const resp = await fetch("https://synapse.arunlabs.com/voices");
const voices = await resp.json();
voices.forEach((v: any) =>
  console.log(`${v.name} (${v.references_count} refs)`),
);
```

---

### POST /voices

Create a new voice by uploading WAV reference samples.

**Request**: `multipart/form-data`

| Field   | Type   | Required | Description                                                        |
| ------- | ------ | -------- | ------------------------------------------------------------------ |
| `name`  | string | yes      | Human-readable voice name                                          |
| `files` | file[] | yes      | 1--10 WAV files, max 50 MB each, minimum 6 seconds of clean speech |

**Response** (201)

```json
{
  "voice_id": "a1b2c3d4-...",
  "name": "narrator",
  "references_count": 2,
  "references": ["ref_001.wav", "ref_002.wav"]
}
```

**Status Codes**

| Code | Description                                                                |
| ---- | -------------------------------------------------------------------------- |
| 201  | Voice created                                                              |
| 400  | Invalid input (wrong file count, file too large, not WAV, audio too short) |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/voices \
  -F "name=narrator" \
  -F "files=@sample1.wav" \
  -F "files=@sample2.wav"
```

```python
import requests

with open("sample1.wav", "rb") as f1, open("sample2.wav", "rb") as f2:
    files = [
        ("files", ("sample1.wav", f1, "audio/wav")),
        ("files", ("sample2.wav", f2, "audio/wav")),
    ]
    resp = requests.post(
        "https://synapse.arunlabs.com/voices",
        data={"name": "narrator"},
        files=files,
    )
voice = resp.json()
print(voice["voice_id"])
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append("name", "narrator");
form.append("files", new Blob([fs.readFileSync("sample1.wav")]), "sample1.wav");
form.append("files", new Blob([fs.readFileSync("sample2.wav")]), "sample2.wav");

const resp = await fetch("https://synapse.arunlabs.com/voices", {
  method: "POST",
  body: form,
});
const voice = await resp.json();
console.log(voice.voice_id);
```

---

### POST /voices/{voice_id}/references

Add reference samples to an existing voice.

**Path Parameters**

| Parameter  | Type   | Description       |
| ---------- | ------ | ----------------- |
| `voice_id` | string | UUID of the voice |

**Request**: `multipart/form-data`

| Field   | Type   | Required | Description                                                        |
| ------- | ------ | -------- | ------------------------------------------------------------------ |
| `files` | file[] | yes      | 1--10 WAV files, max 50 MB each, minimum 6 seconds of clean speech |

**Response** (200)

```json
{
  "voice_id": "a1b2c3d4-...",
  "name": "narrator",
  "references_count": 3,
  "references": ["ref_001.wav", "ref_002.wav", "ref_003.wav"]
}
```

**Status Codes**

| Code | Description      |
| ---- | ---------------- |
| 200  | References added |
| 400  | Invalid input    |
| 404  | Voice not found  |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/voices/a1b2c3d4-.../references \
  -F "files=@extra_sample.wav"
```

```python
import requests

with open("extra_sample.wav", "rb") as f:
    files = [("files", ("extra.wav", f, "audio/wav"))]
    resp = requests.post(
        "https://synapse.arunlabs.com/voices/a1b2c3d4-.../references",
        files=files,
    )
print(resp.json()["references_count"])
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append(
  "files",
  new Blob([fs.readFileSync("extra_sample.wav")]),
  "extra_sample.wav",
);

const resp = await fetch(
  "https://synapse.arunlabs.com/voices/a1b2c3d4-.../references",
  {
    method: "POST",
    body: form,
  },
);
const data = await resp.json();
console.log(data.references_count);
```

---

### DELETE /voices/{voice_id}

Permanently delete a voice and all its reference files.

**Path Parameters**

| Parameter  | Type   | Description       |
| ---------- | ------ | ----------------- |
| `voice_id` | string | UUID of the voice |

**Response** (200)

```json
{
  "status": "deleted",
  "voice_id": "a1b2c3d4-..."
}
```

**Status Codes**

| Code | Description     |
| ---- | --------------- |
| 200  | Voice deleted   |
| 404  | Voice not found |

**Examples**

```bash
curl -X DELETE https://synapse.arunlabs.com/voices/a1b2c3d4-...
```

```python
import requests

resp = requests.delete("https://synapse.arunlabs.com/voices/a1b2c3d4-...")
print(resp.json()["status"])  # "deleted"
```

```typescript
const resp = await fetch("https://synapse.arunlabs.com/voices/a1b2c3d4-...", {
  method: "DELETE",
});
const data = await resp.json();
console.log(data.status); // "deleted"
```

---

## Text-to-Speech (TTS)

Proxied to Chatterbox Turbo (350M params, MIT license, zero-shot voice cloning, 23 languages). Default voice: `Alice.wav`. Backend timeout: 120 seconds.

When a `voice_id` is provided, the gateway performs a two-step flow internally:

1. Uploads the voice reference WAV to Chatterbox via `POST /upload_reference`
2. Sends `POST /tts` with the uploaded filename

Uploaded filenames are cached per `voice_id` to avoid redundant uploads.

### POST /tts/synthesize

Generate speech from text. Optionally clone a voice.

**Request**: `application/json`

| Field             | Type        | Default    | Description                                        |
| ----------------- | ----------- | ---------- | -------------------------------------------------- |
| `text`            | string      | (required) | Text to synthesize (1--5000 chars)                 |
| `voice_id`        | string/null | null       | Voice to clone; uses default voice if null         |
| `language`        | string      | `"en"`     | ISO 639-1 language code (see `GET /tts/languages`) |
| `speed`           | float       | 1.0        | Speed factor (0.5--2.0)                            |
| `split_sentences` | bool        | true       | Split text into sentences for better prosody       |

**Response**: `audio/wav` binary (Content-Disposition: `synapse_tts.wav`)

**Status Codes**

| Code | Description                                         |
| ---- | --------------------------------------------------- |
| 200  | Audio returned                                      |
| 400  | Invalid request (text too long, speed out of range) |
| 404  | Voice not found                                     |
| 502  | Failed to upload reference to Chatterbox            |
| 503  | Chatterbox backend unavailable                      |
| 504  | Synthesis timed out                                 |

**Examples**

Default voice:

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

With voice cloning:

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

```python
import requests

# Default voice
resp = requests.post(
    "https://synapse.arunlabs.com/tts/synthesize",
    json={"text": "Hello from Synapse", "language": "en", "speed": 1.0},
)
with open("speech.wav", "wb") as f:
    f.write(resp.content)

# Voice cloning
resp = requests.post(
    "https://synapse.arunlabs.com/tts/synthesize",
    json={"text": "Hello from Synapse", "voice_id": "a1b2c3d4-...", "language": "en"},
)
with open("cloned.wav", "wb") as f:
    f.write(resp.content)
```

```typescript
import fs from "node:fs";

// Default voice
const resp = await fetch("https://synapse.arunlabs.com/tts/synthesize", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    text: "Hello from Synapse",
    language: "en",
    speed: 1.0,
  }),
});
fs.writeFileSync("speech.wav", Buffer.from(await resp.arrayBuffer()));

// Voice cloning
const cloned = await fetch("https://synapse.arunlabs.com/tts/synthesize", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    text: "Hello from Synapse",
    voice_id: "a1b2c3d4-...",
  }),
});
fs.writeFileSync("cloned.wav", Buffer.from(await cloned.arrayBuffer()));
```

---

### POST /tts/stream

Stream TTS audio. Behavior depends on whether voice cloning is requested:

- **Without `voice_id`**: Uses Chatterbox `/v1/audio/speech` (OpenAI-compatible, chunked streaming).
- **With `voice_id`**: Falls back to `/tts` endpoint (two-step upload flow, full response).

**Request**: `application/json`

Same schema as [`POST /tts/synthesize`](#post-ttssynthesize).

**Response**: `audio/wav` binary

**Status Codes**

| Code | Description                              |
| ---- | ---------------------------------------- |
| 200  | Audio returned                           |
| 400  | Invalid request                          |
| 404  | Voice not found                          |
| 502  | Failed to upload reference to Chatterbox |
| 503  | Chatterbox backend unavailable           |
| 504  | Synthesis timed out                      |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/tts/stream \
  -H "Content-Type: application/json" \
  -d '{"text": "Streaming test", "language": "en", "speed": 1.0}' \
  --output streamed.wav
```

```python
import requests

resp = requests.post(
    "https://synapse.arunlabs.com/tts/stream",
    json={"text": "Streaming test", "language": "en"},
    stream=True,
)
with open("streamed.wav", "wb") as f:
    for chunk in resp.iter_content(chunk_size=4096):
        f.write(chunk)
```

```typescript
import fs from "node:fs";

const resp = await fetch("https://synapse.arunlabs.com/tts/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text: "Streaming test", language: "en" }),
});
fs.writeFileSync("streamed.wav", Buffer.from(await resp.arrayBuffer()));
```

---

### POST /tts/interpolate

Blend multiple voices with weights and synthesize. Currently uses the highest-weighted voice (Chatterbox does not natively support interpolation).

**Request**: `application/json`

| Field      | Type          | Default    | Description                             |
| ---------- | ------------- | ---------- | --------------------------------------- |
| `text`     | string        | (required) | Text to synthesize (1--5000 chars)      |
| `voices`   | VoiceWeight[] | (required) | 2--5 voices with weights summing to 1.0 |
| `language` | string        | `"en"`     | ISO 639-1 language code                 |
| `speed`    | float         | 1.0        | Speed factor (0.5--2.0)                 |

**VoiceWeight schema**

| Field      | Type   | Description                |
| ---------- | ------ | -------------------------- |
| `voice_id` | string | UUID of the voice          |
| `weight`   | float  | Weight between 0.0 and 1.0 |

Weights must sum to 1.0 (tolerance: +/-0.01).

**Response**: `audio/wav` binary

**Status Codes**

| Code | Description                                           |
| ---- | ----------------------------------------------------- |
| 200  | Audio returned                                        |
| 400  | Invalid request (bad weights, wrong number of voices) |
| 404  | One or more voices not found                          |
| 502  | Failed to upload reference to Chatterbox              |
| 503  | Chatterbox backend unavailable                        |

**Examples**

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

```python
import requests

resp = requests.post(
    "https://synapse.arunlabs.com/tts/interpolate",
    json={
        "text": "Blended voice output",
        "voices": [
            {"voice_id": "voice-1-uuid", "weight": 0.7},
            {"voice_id": "voice-2-uuid", "weight": 0.3},
        ],
        "language": "en",
        "speed": 1.0,
    },
)
with open("interpolated.wav", "wb") as f:
    f.write(resp.content)
```

```typescript
import fs from "node:fs";

const resp = await fetch("https://synapse.arunlabs.com/tts/interpolate", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    text: "Blended voice output",
    voices: [
      { voice_id: "voice-1-uuid", weight: 0.7 },
      { voice_id: "voice-2-uuid", weight: 0.3 },
    ],
    language: "en",
    speed: 1.0,
  }),
});
fs.writeFileSync("interpolated.wav", Buffer.from(await resp.arrayBuffer()));
```

---

### GET /tts/languages

List all supported TTS languages.

**Response**

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

23 languages total: ar, cs, da, de, en, es, fi, fr, hi, hu, it, ja, ko, nb, nl, pl, pt, ro, ru, sv, tr, uk, zh.

**Status Codes**

| Code | Description            |
| ---- | ---------------------- |
| 200  | Language list returned |

**Examples**

```bash
curl https://synapse.arunlabs.com/tts/languages
```

```python
import requests

resp = requests.get("https://synapse.arunlabs.com/tts/languages")
for lang in resp.json():
    print(f"{lang['code']}: {lang['name']}")
```

```typescript
const resp = await fetch("https://synapse.arunlabs.com/tts/languages");
const languages = await resp.json();
languages.forEach((l: any) => console.log(`${l.code}: ${l.name}`));
```

---

## Speech-to-Text (STT)

Proxied to faster-whisper (Whisper large-v3-turbo, int8 quantized, CPU). Backend timeout: 120 seconds.

### POST /stt/transcribe

Full transcription of an audio file.

**Request**: `multipart/form-data`

| Field             | Type        | Default    | Description                            |
| ----------------- | ----------- | ---------- | -------------------------------------- |
| `file`            | file        | (required) | Audio file (WAV, MP3, FLAC, OGG, etc.) |
| `language`        | string/null | null       | ISO 639-1 code; auto-detected if null  |
| `word_timestamps` | bool        | false      | Include per-word timestamps in output  |

**Response**

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

The `words` array is only present when `word_timestamps` is true.

**Status Codes**

| Code | Description                                      |
| ---- | ------------------------------------------------ |
| 200  | Transcription returned                           |
| 400  | Invalid input (missing file, unsupported format) |
| 503  | STT backend unavailable                          |
| 504  | Transcription timed out                          |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/stt/transcribe \
  -F "file=@recording.wav" \
  -F "language=en" \
  -F "word_timestamps=true"
```

```python
import requests

with open("recording.wav", "rb") as f:
    resp = requests.post(
        "https://synapse.arunlabs.com/stt/transcribe",
        files={"file": ("recording.wav", f, "audio/wav")},
        data={"language": "en", "word_timestamps": "true"},
    )
result = resp.json()
print(result["text"])
for seg in result["segments"]:
    print(f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}")
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append(
  "file",
  new Blob([fs.readFileSync("recording.wav")]),
  "recording.wav",
);
form.append("language", "en");
form.append("word_timestamps", "true");

const resp = await fetch("https://synapse.arunlabs.com/stt/transcribe", {
  method: "POST",
  body: form,
});
const result = await resp.json();
console.log(result.text);
```

---

### POST /stt/detect-language

Detect the spoken language in an audio file.

**Request**: `multipart/form-data`

| Field  | Type | Default    | Description |
| ------ | ---- | ---------- | ----------- |
| `file` | file | (required) | Audio file  |

**Response**

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

**Status Codes**

| Code | Description             |
| ---- | ----------------------- |
| 200  | Language detected       |
| 400  | Invalid input           |
| 503  | STT backend unavailable |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/stt/detect-language \
  -F "file=@recording.wav"
```

```python
import requests

with open("recording.wav", "rb") as f:
    resp = requests.post(
        "https://synapse.arunlabs.com/stt/detect-language",
        files={"file": ("recording.wav", f, "audio/wav")},
    )
result = resp.json()
print(f"Detected: {result['detected_language']} ({result['probability']:.0%})")
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append(
  "file",
  new Blob([fs.readFileSync("recording.wav")]),
  "recording.wav",
);

const resp = await fetch("https://synapse.arunlabs.com/stt/detect-language", {
  method: "POST",
  body: form,
});
const result = await resp.json();
console.log(`Detected: ${result.detected_language} (${result.probability})`);
```

---

### POST /stt/stream

Stream transcription segments as Server-Sent Events (SSE).

**Request**: `multipart/form-data`

| Field      | Type        | Default    | Description                           |
| ---------- | ----------- | ---------- | ------------------------------------- |
| `file`     | file        | (required) | Audio file                            |
| `language` | string/null | null       | ISO 639-1 code; auto-detected if null |

**Response**: `text/event-stream`

```
event: segment
data: {"id": 1, "text": "Hello", "start": 0.0, "end": 0.4, "words": [...]}

event: segment
data: {"id": 2, "text": " from Synapse.", "start": 0.4, "end": 2.5, "words": [...]}

event: done
data: {}
```

**Status Codes**

| Code | Description             |
| ---- | ----------------------- |
| 200  | SSE stream started      |
| 400  | Invalid input           |
| 503  | STT backend unavailable |

**Examples**

```bash
curl -N -X POST https://synapse.arunlabs.com/stt/stream \
  -F "file=@recording.wav" \
  -F "language=en"
```

```python
import requests

with open("recording.wav", "rb") as f:
    resp = requests.post(
        "https://synapse.arunlabs.com/stt/stream",
        files={"file": ("recording.wav", f, "audio/wav")},
        data={"language": "en"},
        stream=True,
    )
for line in resp.iter_lines(decode_unicode=True):
    if line.startswith("data:"):
        print(line[5:].strip())
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append(
  "file",
  new Blob([fs.readFileSync("recording.wav")]),
  "recording.wav",
);
form.append("language", "en");

const resp = await fetch("https://synapse.arunlabs.com/stt/stream", {
  method: "POST",
  body: form,
});
const reader = resp.body!.getReader();
const decoder = new TextDecoder();
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  console.log(decoder.decode(value));
}
```

---

## Speaker Analysis

Proxied to pyannote.audio 3.1 (speaker-diarization-3.1 + embedding model, CPU). Backend timeout: 120 seconds. Requires HuggingFace token for gated model access.

### POST /speakers/diarize

Identify who spoke when in an audio file.

**Request**: `multipart/form-data`

| Field          | Type     | Default    | Description                         |
| -------------- | -------- | ---------- | ----------------------------------- |
| `file`         | file     | (required) | Audio file                          |
| `num_speakers` | int/null | null       | Exact number of speakers (if known) |
| `min_speakers` | int/null | null       | Minimum expected speakers           |
| `max_speakers` | int/null | null       | Maximum expected speakers           |

**Response**

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

**Status Codes**

| Code | Description                 |
| ---- | --------------------------- |
| 200  | Diarization complete        |
| 400  | Invalid input               |
| 503  | Speaker backend unavailable |
| 504  | Diarization timed out       |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/speakers/diarize \
  -F "file=@meeting.wav" \
  -F "min_speakers=2" \
  -F "max_speakers=5"
```

```python
import requests

with open("meeting.wav", "rb") as f:
    resp = requests.post(
        "https://synapse.arunlabs.com/speakers/diarize",
        files={"file": ("meeting.wav", f, "audio/wav")},
        data={"min_speakers": "2", "max_speakers": "5"},
    )
result = resp.json()
print(f"Found {result['num_speakers']} speakers")
for seg in result["segments"]:
    print(f"  {seg['speaker']}: {seg['start']:.1f}s - {seg['end']:.1f}s")
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append("file", new Blob([fs.readFileSync("meeting.wav")]), "meeting.wav");
form.append("min_speakers", "2");
form.append("max_speakers", "5");

const resp = await fetch("https://synapse.arunlabs.com/speakers/diarize", {
  method: "POST",
  body: form,
});
const result = await resp.json();
console.log(`Found ${result.num_speakers} speakers`);
result.segments.forEach((s: any) =>
  console.log(`  ${s.speaker}: ${s.start.toFixed(1)}s - ${s.end.toFixed(1)}s`),
);
```

---

### POST /speakers/verify

Compare two audio samples to determine if the same person is speaking.

**Request**: `multipart/form-data`

| Field   | Type | Default    | Description         |
| ------- | ---- | ---------- | ------------------- |
| `file1` | file | (required) | First audio sample  |
| `file2` | file | (required) | Second audio sample |

**Response**

```json
{
  "is_same_speaker": true,
  "similarity_score": 0.8742,
  "threshold": 0.5
}
```

`is_same_speaker` is true when `similarity_score >= threshold`.

**Status Codes**

| Code | Description                  |
| ---- | ---------------------------- |
| 200  | Verification complete        |
| 400  | Invalid input (missing file) |
| 503  | Speaker backend unavailable  |
| 504  | Verification timed out       |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/speakers/verify \
  -F "file1=@sample_a.wav" \
  -F "file2=@sample_b.wav"
```

```python
import requests

with open("sample_a.wav", "rb") as f1, open("sample_b.wav", "rb") as f2:
    files = {
        "file1": ("sample_a.wav", f1, "audio/wav"),
        "file2": ("sample_b.wav", f2, "audio/wav"),
    }
    resp = requests.post("https://synapse.arunlabs.com/speakers/verify", files=files)
result = resp.json()
if result["is_same_speaker"]:
    print(f"Same speaker (score: {result['similarity_score']:.2f})")
else:
    print(f"Different speakers (score: {result['similarity_score']:.2f})")
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append(
  "file1",
  new Blob([fs.readFileSync("sample_a.wav")]),
  "sample_a.wav",
);
form.append(
  "file2",
  new Blob([fs.readFileSync("sample_b.wav")]),
  "sample_b.wav",
);

const resp = await fetch("https://synapse.arunlabs.com/speakers/verify", {
  method: "POST",
  body: form,
});
const result = await resp.json();
console.log(result.is_same_speaker ? "Same speaker" : "Different speakers");
console.log(
  `Score: ${result.similarity_score}, Threshold: ${result.threshold}`,
);
```

---

## Audio Processing

Proxied to DeepFilterNet3 (noise reduction) and ffmpeg (format conversion), CPU. Backend timeout: 60 seconds.

### POST /audio/denoise

Remove background noise from an audio file using DeepFilterNet3.

**Request**: `multipart/form-data`

| Field  | Type | Default    | Description           |
| ------ | ---- | ---------- | --------------------- |
| `file` | file | (required) | Audio file to denoise |

**Response**: `audio/wav` binary (Content-Disposition: `denoised.wav`)

**Status Codes**

| Code | Description               |
| ---- | ------------------------- |
| 200  | Denoised audio returned   |
| 400  | Invalid input             |
| 503  | Audio backend unavailable |
| 504  | Processing timed out      |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@noisy_recording.wav" \
  --output denoised.wav
```

```python
import requests

with open("noisy_recording.wav", "rb") as f:
    resp = requests.post(
        "https://synapse.arunlabs.com/audio/denoise",
        files={"file": ("noisy.wav", f, "audio/wav")},
    )
with open("denoised.wav", "wb") as f:
    f.write(resp.content)
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append(
  "file",
  new Blob([fs.readFileSync("noisy_recording.wav")]),
  "noisy.wav",
);

const resp = await fetch("https://synapse.arunlabs.com/audio/denoise", {
  method: "POST",
  body: form,
});
fs.writeFileSync("denoised.wav", Buffer.from(await resp.arrayBuffer()));
```

---

### POST /audio/convert

Convert between audio formats using ffmpeg.

**Request**: `multipart/form-data`

| Field           | Type        | Default    | Description                                   |
| --------------- | ----------- | ---------- | --------------------------------------------- |
| `file`          | file        | (required) | Input audio file                              |
| `output_format` | string      | `"wav"`    | Target format: `wav`, `mp3`, `flac`, `ogg`    |
| `sample_rate`   | int/null    | null       | Target sample rate in Hz (e.g., 44100, 16000) |
| `bitrate`       | string/null | null       | Target bitrate (e.g., `"192k"`, `"320k"`)     |

**Response**: Audio binary in the requested format

**Status Codes**

| Code | Description                        |
| ---- | ---------------------------------- |
| 200  | Converted audio returned           |
| 400  | Invalid input (unsupported format) |
| 503  | Audio backend unavailable          |
| 504  | Conversion timed out               |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/audio/convert \
  -F "file=@input.wav" \
  -F "output_format=mp3" \
  -F "sample_rate=44100" \
  -F "bitrate=192k" \
  --output output.mp3
```

```python
import requests

with open("input.wav", "rb") as f:
    resp = requests.post(
        "https://synapse.arunlabs.com/audio/convert",
        files={"file": ("input.wav", f, "audio/wav")},
        data={"output_format": "mp3", "sample_rate": "44100", "bitrate": "192k"},
    )
with open("output.mp3", "wb") as f:
    f.write(resp.content)
```

```typescript
import fs from "node:fs";

const form = new FormData();
form.append("file", new Blob([fs.readFileSync("input.wav")]), "input.wav");
form.append("output_format", "mp3");
form.append("sample_rate", "44100");
form.append("bitrate", "192k");

const resp = await fetch("https://synapse.arunlabs.com/audio/convert", {
  method: "POST",
  body: form,
});
fs.writeFileSync("output.mp3", Buffer.from(await resp.arrayBuffer()));
```

---

## Embeddings

Proxied to llama-embed (snowflake-arctic-embed2, CPU, 1024 dimensions). Backend timeout: 60 seconds.

### POST /v1/embeddings

Generate text embeddings. OpenAI-compatible endpoint.

**Request**: `application/json`

| Field   | Type            | Default    | Description                                           |
| ------- | --------------- | ---------- | ----------------------------------------------------- |
| `model` | string          | (required) | Model name (e.g., `"snowflake-arctic-embed2:latest"`) |
| `input` | string/string[] | (required) | Text or array of texts to embed                       |

**Response**

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.0123, -0.0456, ...]
    }
  ],
  "model": "snowflake-arctic-embed2:latest",
  "usage": {
    "prompt_tokens": 3,
    "total_tokens": 3
  }
}
```

Each embedding is a 1024-dimensional float vector.

**Status Codes**

| Code | Description                   |
| ---- | ----------------------------- |
| 200  | Embeddings returned           |
| 400  | Invalid input                 |
| 503  | Embedding backend unavailable |
| 504  | Request timed out             |

**Examples**

```bash
curl -X POST https://synapse.arunlabs.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model": "snowflake-arctic-embed2:latest", "input": "test text"}'
```

```python
import requests

resp = requests.post(
    "https://synapse.arunlabs.com/v1/embeddings",
    json={"model": "snowflake-arctic-embed2:latest", "input": "test text"},
)
data = resp.json()
embedding = data["data"][0]["embedding"]
print(f"Dimensions: {len(embedding)}")  # 1024
```

```typescript
const resp = await fetch("https://synapse.arunlabs.com/v1/embeddings", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "snowflake-arctic-embed2:latest",
    input: "test text",
  }),
});
const data = await resp.json();
console.log(`Dimensions: ${data.data[0].embedding.length}`); // 1024
```

---

## Error Reference

| HTTP Code | Meaning               | Typical Cause                                                               |
| --------- | --------------------- | --------------------------------------------------------------------------- |
| 200       | Success               | Request completed normally                                                  |
| 201       | Created               | Voice created via `POST /voices`                                            |
| 400       | Bad Request           | Invalid input, wrong file count, file too large, unsupported format         |
| 404       | Not Found             | Voice ID does not exist                                                     |
| 422       | Validation Error      | Request failed FastAPI validation (e.g., text too long, speed out of range) |
| 500       | Internal Server Error | Backend processing failure (model error, unexpected exception)              |
| 502       | Bad Gateway           | Backend returned an unexpected response (e.g., Chatterbox upload failed)    |
| 503       | Service Unavailable   | Backend unreachable or circuit breaker open                                 |
| 504       | Gateway Timeout       | Backend did not respond within the configured timeout                       |

**Gateway errors** (503, 504, 500) return:

```json
{
  "error": "Backend unavailable",
  "detail": "Connection refused"
}
```

The `error` field is a short category (`"Backend unavailable"`, `"Backend timeout"`, `"Internal server error"`) and `detail` contains the specific error message.

**FastAPI validation errors** (422) return:

```json
{
  "detail": [
    {
      "loc": ["body", "text"],
      "msg": "ensure this value has at most 5000 characters",
      "type": "value_error"
    }
  ]
}
```

---

## Circuit Breaker

The gateway implements a per-backend circuit breaker to prevent cascading failures.

| Parameter         | Value                                                        |
| ----------------- | ------------------------------------------------------------ |
| Failure threshold | 5 consecutive failures                                       |
| Block duration    | 30 seconds                                                   |
| Recovery mode     | Half-open (single probe request allowed after block expires) |
| Retry attempts    | 3 per request                                                |
| Retry strategy    | Exponential backoff: 0.5s, 1s, 2s                            |
| Retry scope       | Connection errors only (not HTTP error responses)            |

**Important**: Both retries and circuit breaker failures are triggered exclusively by connection-level errors (`ConnectError`, `ConnectTimeout`). An HTTP 500 from a backend is **not** treated as a failure — the circuit breaker records it as a success (the backend is reachable). This means a backend returning 500 errors will never trip the circuit breaker; only network-level failures (connection refused, DNS errors, timeouts) will.

**State transitions**:

```
CLOSED  --[5 consecutive failures]--> OPEN
OPEN    --[30s elapsed]--------------> HALF-OPEN
HALF-OPEN --[probe succeeds]---------> CLOSED
HALF-OPEN --[probe fails]-----------> OPEN
```

When the circuit is open, the gateway returns `503 Service Unavailable` without contacting the backend.

---

## Timeouts

Per-backend request timeouts:

| Backend          | Timeout | Routes                 |
| ---------------- | ------- | ---------------------- |
| Chatterbox TTS   | 120s    | `/tts/*`               |
| whisper-stt      | 120s    | `/stt/*`               |
| pyannote-speaker | 120s    | `/speakers/*`          |
| deepfilter-audio | 60s     | `/audio/*`             |
| llama-embed      | 60s     | `/v1/embeddings`       |
| vLLM (future)    | 300s    | `/v1/chat/completions` |

Requests exceeding the timeout return `504 Gateway Timeout`.

---

## Configuration

### backends.yaml

The gateway reads its backend registry from a YAML config file (deployed as a Kubernetes ConfigMap):

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

Each backend entry has:

| Field    | Type   | Description                                                  |
| -------- | ------ | ------------------------------------------------------------ |
| `url`    | string | Internal cluster URL of the backend service                  |
| `type`   | string | Backend type identifier (used for type-specific proxy logic) |
| `health` | string | Health check endpoint path                                   |

The `routes` section maps URL path patterns to backend names.

### Environment Variables

| Variable                      | Default                 | Description                                         |
| ----------------------------- | ----------------------- | --------------------------------------------------- |
| `SYNAPSE_GATEWAY_CONFIG_PATH` | `/config/backends.yaml` | Path to the backend registry YAML file              |
| `SYNAPSE_VOICE_LIBRARY_DIR`   | `/data/voices`          | Directory for voice reference sample storage        |
| `SYNAPSE_LOG_LEVEL`           | `INFO`                  | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
