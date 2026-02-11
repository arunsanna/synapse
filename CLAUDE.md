# Synapse — Claude Context

## What This Is

Centralized LLM inference gateway for ArunLabs Forge cluster. Routes OpenAI-compatible API requests to the optimal backend: Ollama (CPU), vLLM (GPU), or specialized TTS/STT services.

## Critical Context

- **Namespace**: `llm-infra` on forge K3s cluster
- **Gateway**: LiteLLM proxy at port 8000 — single entry point for all apps
- **CPU Backend**: Ollama (embeddings, small LLMs, fallback)
- **GPU Backend**: vLLM via `arunlabs-vllm` sibling project
- **RTX 5090 (Blackwell)**: Sleep mode has known bugs on this arch — test before relying on it
- **RAM**: 196GB — target 80% max utilization (157GB budget), NOT 100%

## Key Design Decisions

1. **Phase 1 = Ollama only** — proves value before adding complexity (DA-12 recommendation)
2. **LiteLLM memory leaks** — deploy with 2 replicas + 24h rolling restart CronJob
3. **vLLM sleep mode** — must validate on RTX 5090 before using in production (DA-1)
4. **Drop Whisper from vLLM** — use Speaches (faster-whisper) as sole STT backend (DA-3)
5. **SGLang** — benchmark against vLLM before committing (DA-4)

## Deployment Target

- **Cluster**: forge (megamind-dc, 172.16.0.191)
- **Access**: `ssh forge` then `sudo kubectl ...`
- **Namespace**: `llm-infra`
- **Ingress**: TBD (`synapse.arunlabs.com` or `llm.arunlabs.com`)
- **Storage**: `local-path` provisioner (k3s default)

## Routing Rules

| Model Request      | Primary Backend    | Fallback     |
| ------------------ | ------------------ | ------------ |
| Embeddings         | Ollama (CPU)       | None         |
| Small LLMs (8B)    | vLLM (GPU)         | Ollama (CPU) |
| Large LLMs (70B+)  | vLLM (GPU)         | None         |
| STT (Whisper)      | Speaches (GPU/CPU) | None         |
| TTS (standard)     | Piper (CPU)        | None         |
| TTS (high quality) | OpenVoice (GPU)    | Piper (CPU)  |

## Related Projects

- `arunlabs-vllm` — standalone vLLM deployment (to be integrated)
- `arunlabs-forge` — the K3s cluster itself
- `coqui-tts-server` — existing TTS server (may be superseded)
- `openvoice` — existing OpenVoice deployment

## Commands

```bash
# Deploy all (from project root)
make deploy

# Deploy phase 1 only
make deploy-phase1

# Health check
make test-health

# View routing
make show-routes
```
