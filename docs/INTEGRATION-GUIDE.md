# Synapse Integration Guide

Practical, end-to-end workflows for integrating with Synapse.

- Base URL: `https://synapse.arunlabs.com`
- Internal URL: `http://synapse-gateway.llm-infra.svc.cluster.local:8000`
- Full schema and status codes: [API.md](API.md)

## Workflow 1: Voice Cloning

### 1) Upload reference samples

```bash
curl -X POST https://synapse.arunlabs.com/voices \
  -F "name=narrator" \
  -F "files=@sample1.wav" \
  -F "files=@sample2.wav"
```

Response includes `voice_id`.

### 2) Synthesize with cloned voice

```bash
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hello from Synapse",
    "voice_id": "a1b2c3d4-...",
    "language": "en",
    "speed": 1.0
  }' \
  --output cloned.wav
```

### 3) Stream TTS

```bash
curl -X POST https://synapse.arunlabs.com/tts/stream \
  -H "Content-Type: application/json" \
  -d '{"text":"Streaming speech sample","language":"en"}' \
  --output streamed.wav
```

Note: when `voice_id` is provided, streaming falls back to full-response clone flow.

### 4) Manage voice library

```bash
# List
curl https://synapse.arunlabs.com/voices

# Add references
curl -X POST https://synapse.arunlabs.com/voices/a1b2c3d4-.../references \
  -F "files=@extra_ref.wav"

# Delete
curl -X DELETE https://synapse.arunlabs.com/voices/a1b2c3d4-...
```

## Workflow 2: Meeting Processing Pipeline

### 1) Denoise recording

```bash
curl -X POST https://synapse.arunlabs.com/audio/denoise \
  -F "file=@meeting.wav" \
  --output meeting_clean.wav
```

### 2) Transcribe with timestamps

```bash
curl -X POST https://synapse.arunlabs.com/stt/transcribe \
  -F "file=@meeting_clean.wav" \
  -F "language=en" \
  -F "word_timestamps=true"
```

### 3) Diarize speakers

```bash
curl -X POST https://synapse.arunlabs.com/speakers/diarize \
  -F "file=@meeting_clean.wav" \
  -F "min_speakers=2" \
  -F "max_speakers=5"
```

### 4) Optionally verify two speakers

```bash
curl -X POST https://synapse.arunlabs.com/speakers/verify \
  -F "file1=@speaker_a.wav" \
  -F "file2=@speaker_b.wav"
```

## Workflow 3: LLM Chat + Model Control

### 1) List available model states

```bash
curl https://synapse.arunlabs.com/models
```

### 2) Load a model explicitly

```bash
curl -X POST https://synapse.arunlabs.com/models/load \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen3-8B-Q4_K_M"}'
```

### 3) Chat via OpenAI-compatible endpoint

```bash
curl -X POST https://synapse.arunlabs.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [
      {"role":"user","content":"Write a kubectl command to list pods"}
    ]
  }'
```

### 4) Unload model when idle

```bash
curl -X POST https://synapse.arunlabs.com/models/unload \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen3-8B-Q4_K_M"}'
```

## Workflow 4: Embeddings

```bash
curl -X POST https://synapse.arunlabs.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{
    "model":"snowflake-arctic-embed2:latest",
    "input":["first string","second string"]
  }'
```

Use response vectors for retrieval, clustering, or semantic search.

## Operational Checks

```bash
# Aggregated health
curl https://synapse.arunlabs.com/health

# Dashboard
open https://synapse.arunlabs.com/
```

Additional references:

- [Architecture](ARCHITECTURE.md)
- [Repository organization](REPOSITORY-ORGANIZATION.md)
