<p align="center">
  <img src="docs/diagrams/synapse-architecture.png" alt="Synapse Architecture" width="700">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <strong>Unified AI Gateway for LLM, TTS, STT, speaker analysis, and audio processing</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://kubernetes.io"><img src="https://img.shields.io/badge/kubernetes-%3E%3D1.26-326CE5?logo=kubernetes&logoColor=white" alt="Kubernetes"></a>
  <a href="https://fastapi.tiangolo.com"><img src="https://img.shields.io/badge/gateway-FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI"></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white" alt="Python 3.11"></a>
</p>

---

## What Synapse Does

Synapse is a custom FastAPI gateway that provides one public endpoint for all AI workloads running in a K3s cluster:

- LLM embeddings and chat
- Text-to-speech with zero-shot voice cloning
- Speech-to-text and language detection
- Speaker diarization and verification
- Audio denoising and format conversion

Instead of exposing each backend separately, Synapse routes requests through a single URL:

- `https://synapse.arunlabs.com`

## Core Capabilities

- Single gateway for 6 backend services
- Voice library management with PVC-backed storage
- Per-backend circuit breakers and retries
- Centralized health aggregation (`GET /health`)
- OpenAI-compatible endpoints for embeddings and chat
- Dashboard UI for backend health, model operations, and live terminal feed (`/`, `/ui`, `/dashboard`)

## API Surface

Synapse currently exposes **22 OpenAPI endpoints** across health, LLM, voice, TTS, STT, speaker, and audio domains.

| Method   | Path                      | Description                                     | Backend          |
| -------- | ------------------------- | ----------------------------------------------- | ---------------- |
| `GET`    | `/health`                 | Aggregated health of all backends               | Gateway          |
| `GET`    | `/voices`                 | List all voices in library                      | Gateway (local)  |
| `POST`   | `/voices`                 | Upload voice reference samples                  | Gateway (local)  |
| `POST`   | `/voices/{id}/references` | Add references to existing voice                | Gateway (local)  |
| `DELETE` | `/voices/{id}`            | Delete a voice                                  | Gateway (local)  |
| `POST`   | `/tts/synthesize`         | Synthesize speech (optional voice cloning)      | Chatterbox TTS   |
| `POST`   | `/tts/stream`             | Stream TTS audio                                | Chatterbox TTS   |
| `POST`   | `/tts/interpolate`        | Blend voices and synthesize                     | Chatterbox TTS   |
| `GET`    | `/tts/languages`          | List supported TTS languages                    | Gateway (static) |
| `POST`   | `/stt/transcribe`         | Full audio transcription                        | whisper-stt      |
| `POST`   | `/stt/detect-language`    | Detect spoken language                          | whisper-stt      |
| `POST`   | `/stt/stream`             | Stream transcription segments (SSE)             | whisper-stt      |
| `POST`   | `/speakers/diarize`       | Speaker diarization                             | pyannote-speaker |
| `POST`   | `/speakers/verify`        | Speaker verification                            | pyannote-speaker |
| `POST`   | `/audio/denoise`          | Remove background noise                         | deepfilter-audio |
| `POST`   | `/audio/convert`          | Convert audio format                            | deepfilter-audio |
| `POST`   | `/v1/embeddings`          | Generate text embeddings                        | llama-embed      |
| `POST`   | `/v1/chat/completions`    | OpenAI-compatible chat completions              | llama-router     |
| `GET`    | `/models`                 | List router model statuses                      | llama-router     |
| `POST`   | `/models/load`            | Load model in llama-router                      | llama-router     |
| `POST`   | `/models/unload`          | Unload model in llama-router                    | llama-router     |
| `GET`    | `/v1/models`              | Aggregate model catalogs across configured LLMs | Gateway          |

UI routes (not part of OpenAPI):

- `GET /`
- `GET /ui`
- `GET /dashboard`
- `GET /dashboard/login?access_token=...` (sets auth cookie)
- `GET /events/terminal` (SSE terminal log feed; requires dashboard auth)

Dashboard and terminal feed endpoints are token-gated. Set `SYNAPSE_DASHBOARD_ACCESS_TOKEN` and log in via `/dashboard/login?access_token=...`.

`/events/terminal` includes `instance` on each event. With `SYNAPSE_TERMINAL_FEED_BUS_MODE=redis`, all gateway replicas share a unified feed.

## Quick Start

### Health

```bash
curl https://synapse.arunlabs.com/health
```

### TTS

```bash
curl -X POST https://synapse.arunlabs.com/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello from Synapse", "language": "en"}' \
  --output speech.wav
```

### Embeddings

```bash
curl -X POST https://synapse.arunlabs.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model": "snowflake-arctic-embed2:latest", "input": "test text"}'
```

### Chat Completions

```bash
curl -X POST https://synapse.arunlabs.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Write a bash one-liner to count files"}]
  }'
```

## Configuration

Synapse reads backend routing from `config/backends.yaml` (mounted in-cluster as ConfigMap).

| Variable                      | Default                 | Description                       |
| ----------------------------- | ----------------------- | --------------------------------- |
| `SYNAPSE_GATEWAY_CONFIG_PATH` | `/config/backends.yaml` | Path to backend registry          |
| `SYNAPSE_VOICE_LIBRARY_DIR`   | `/data/voices`          | Voice reference storage directory |
| `SYNAPSE_MODEL_PROFILES_PATH` | `/data/voices/model-profiles.json` | Per-model generation profile storage |
| `SYNAPSE_LLAMA_ROUTER_DEPLOYMENT_NAMESPACE` | `llm-infra` | Namespace of router deployment for runtime reconfigure |
| `SYNAPSE_LLAMA_ROUTER_DEPLOYMENT_NAME` | `llama-router` | Deployment name for runtime reconfigure |
| `SYNAPSE_LLAMA_ROUTER_CONTAINER_NAME` | `llama-server` | Container name in router deployment |
| `SYNAPSE_RUNTIME_RECONFIGURE_TIMEOUT_SECONDS` | `300` | Max seconds to wait for runtime rollout during load |
| `SYNAPSE_LOG_LEVEL`           | `INFO`                  | Logging level                     |
| `SYNAPSE_DASHBOARD_ACCESS_TOKEN` | _unset_ | Required token for `/dashboard` and `/events/terminal` access |
| `SYNAPSE_DASHBOARD_ACCESS_COOKIE_NAME` | `synapse_dash_token` | HttpOnly dashboard session cookie name |
| `SYNAPSE_DASHBOARD_COOKIE_SECURE` | `false` | Set `true` in production HTTPS |
| `SYNAPSE_TERMINAL_FEED_MODE` | `mock` | `live` enables SSE feed from gateway logs |
| `SYNAPSE_TERMINAL_FEED_BUFFER_SIZE` | `500` | In-memory terminal backlog size |
| `SYNAPSE_TERMINAL_FEED_SUBSCRIBER_QUEUE_SIZE` | `200` | Per-client queue bound before dropping oldest |
| `SYNAPSE_TERMINAL_FEED_BACKLOG_LINES` | `80` | Initial lines sent when SSE connects |
| `SYNAPSE_TERMINAL_FEED_KEEPALIVE_SECONDS` | `15` | SSE keepalive cadence |
| `SYNAPSE_TERMINAL_FEED_MAX_LINE_CHARS` | `1200` | Max characters per streamed log line |
| `SYNAPSE_TERMINAL_FEED_DEFAULT_LEVEL` | `INFO` | Default minimum level for feed filtering |
| `SYNAPSE_TERMINAL_FEED_REDACT_EXTRA_PATTERNS` | _unset_ | Extra `||`-separated regex patterns to redact |
| `SYNAPSE_TERMINAL_FEED_BUS_MODE` | `local` | `local` for per-instance feed, `redis` for shared multi-replica feed |
| `SYNAPSE_TERMINAL_FEED_REDIS_URL` | _unset_ | Redis DSN used when bus mode is `redis` |
| `SYNAPSE_TERMINAL_FEED_REDIS_CHANNEL` | `synapse:terminal_feed` | Pub/sub channel for distributed feed events |
| `SYNAPSE_TERMINAL_FEED_REDIS_CONNECT_TIMEOUT_SECONDS` | `5` | Redis socket connect timeout |
| `SYNAPSE_INSTANCE_ID` | `HOSTNAME` | Instance identifier stamped into terminal events |

## Deployment Commands

| Target                | Description                                          |
| --------------------- | ---------------------------------------------------- |
| `make deploy`         | Deploy all services (infra + all apps)              |
| `make deploy-phase1`  | Deploy infra + embeddings + TTS + gateway           |
| `make deploy-gateway-secrets` | Apply gateway auth/terminal-bus secrets (requires editing template first) |
| `make deploy-llm`     | Deploy llama-router                                 |
| `make deploy-terminal-feed-bus` | Deploy Redis bus for multi-replica terminal feed |
| `make deploy-stt`     | Deploy whisper-stt                                  |
| `make deploy-speaker` | Deploy pyannote-speaker                             |
| `make deploy-audio`   | Deploy deepfilter-audio                             |
| `make build-gateway`  | Build and push gateway image                        |
| `make release-gateway-remote` | Build+push on Forge over SSH, then deploy pinned digest |
| `make test-health`    | Cluster + gateway health checks                     |
| `make test-embed`     | Test embedding endpoint                             |
| `make test-tts`       | Test TTS endpoint                                   |
| `make logs`           | Tail logs for all Synapse services                  |
| `make logs-feed-bus`  | Tail terminal feed Redis bus logs                   |
| `make show-routes`    | Print configured route map                          |
| `make validate`       | Validate manifests with `kubectl --dry-run=client`  |
| `make clean`          | Delete Synapse namespace (destructive)              |

Before first gateway deploy, copy/edit `manifests/examples/gateway-secrets.example.yaml` and run:

```bash
make deploy-gateway-secrets GATEWAY_SECRETS_FILE=manifests/examples/gateway-secrets.example.yaml
```

For Forge-first releases (no local docker build), run:

```bash
make release-gateway-remote FORGE_HOST=forge
```

Optional overrides:

- `GATEWAY_REMOTE_TAG=<tag>` to choose the pushed image tag (default is UTC timestamp)
- `REMOTE_DIR=/path/on/forge` to control remote sync/build directory

## Repository Layout

```text
synapse/
├── gateway/                  # FastAPI gateway
├── config/                   # Backend route registry
├── manifests/                # Kubernetes manifests (apps + infra)
├── scripts/                  # Health/check scripts
├── docs/
│   ├── API.md
│   ├── INTEGRATION-GUIDE.md
│   ├── ARCHITECTURE.md
│   ├── REPOSITORY-ORGANIZATION.md
│   ├── diagrams/
│   └── archive/
├── archive/                  # Superseded docs/assets
├── CHANGELOG.md
├── CONTRIBUTING.md
├── Makefile
└── LICENSE
```

## Documentation

- [API Reference](docs/API.md)
- [Integration Guide](docs/INTEGRATION-GUIDE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Repository Organization](docs/REPOSITORY-ORGANIZATION.md)
- [Diagram Assets](docs/diagrams/)
- [Archive Notes](archive/README.md)

## License

[MIT](LICENSE)
