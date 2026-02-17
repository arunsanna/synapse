# Synapse Architecture

Synapse is a single FastAPI gateway that fronts LLM, TTS, STT, speaker-analysis, and audio-processing backends running in Kubernetes.

## System Topology

```mermaid
flowchart LR
    client[Clients\nApps, Agents, CLI] -->|HTTPS| ingress[Ingress\nsynapse.arunlabs.com]
    ingress --> gateway[Synapse Gateway\nFastAPI]

    gateway --> embed[llama-embed\n/v1/embeddings]
    gateway --> llm[llama-router\n/v1/chat/completions\n/models/*]
    gateway --> tts[chatterbox-tts\n/tts/*]
    gateway --> stt[whisper-stt\n/stt/*]
    gateway --> spk[pyannote-speaker\n/speakers/*]
    gateway --> audio[deepfilter-audio\n/audio/*]

    gateway --> voices[(PVC: synapse-voices\nvoice library)]
    embed --> models[(PVC: synapse-models)]
    llm --> models
    tts --> ttscache[(PVC: chatterbox model/hf cache)]
    stt --> sttcache[(PVC: whisper model cache)]
    spk --> spkcache[(PVC: pyannote model cache)]
```

## Request Routing

```mermaid
flowchart TD
    A[Incoming request] --> B{Path match}
    B -->|/v1/embeddings| E[llama-embed]
    B -->|/v1/chat/completions| F[llama-router]
    B -->|/models* or /v1/models| F
    B -->|/tts/*| G[chatterbox-tts]
    B -->|/stt/*| H[whisper-stt]
    B -->|/speakers/*| I[pyannote-speaker]
    B -->|/audio/*| J[deepfilter-audio]
    B -->|/voices*| K[Local voice manager]
```

## Voice Cloning Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant G as Gateway
    participant V as Voice PVC
    participant T as Chatterbox

    C->>G: POST /voices (WAV refs)
    G->>V: Persist voice refs + metadata
    G-->>C: voice_id

    C->>G: POST /tts/synthesize (voice_id + text)
    G->>V: Resolve reference files
    G->>T: POST /upload_reference
    T-->>G: uploaded filename
    G->>T: POST /tts (reference_audio_filename)
    T-->>G: WAV audio
    G-->>C: audio/wav
```

## Reliability Controls

- Per-backend circuit breaker: opens after 5 connection failures, cools down for 30 seconds.
- Request retries (connection errors only): 0.5s, 1s, 2s backoff.
- Timeout profiles by backend type (`llm`, `tts`, `stt`, `speaker`, `audio`, `embeddings`).

## Runtime Entry Points

- API docs: `GET /docs`
- Health: `GET /health`
- Dashboard UI: `GET /`, `GET /ui`, `GET /dashboard`
