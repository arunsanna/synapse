# Synapse

> Centralized LLM Inference Gateway for ArunLabs Forge — intelligent routing between Ollama (CPU), vLLM (GPU), and TTS/STT services on K3s

## Overview

Synapse is the neural connection point for all AI inference on the ArunLabs homelab. It provides a **unified OpenAI-compatible API** that intelligently routes requests to the optimal backend based on model type, hardware availability, and load.

```
┌─────────────────────────────┐
│  Applications               │
│  (Jarvis Cortex, Agents)    │
└──────────┬──────────────────┘
           │ OpenAI-compatible API
┌──────────▼──────────────────┐
│  Synapse (LiteLLM Gateway)  │
│  Routing / Fallback / Logs  │
│  Port: 8000                 │
└──┬───────────┬──────────┬───┘
   │           │          │
┌──▼───┐  ┌───▼──┐  ┌────▼────┐
│Ollama│  │ vLLM │  │TTS/STT  │
│(CPU) │  │(GPU) │  │Services │
└──────┘  └──────┘  └─────────┘
```

## Hardware

- **GPU**: RTX 5090 (32GB GDDR7, Blackwell)
- **CPU**: 32 cores
- **RAM**: 196GB DDR5
- **Platform**: Single-node K3s (forge)

## Architecture

| Component         | Role                                | Backend     |
| ----------------- | ----------------------------------- | ----------- |
| **LiteLLM Proxy** | API gateway, routing, fallback      | Python      |
| **Ollama**        | CPU inference, embeddings, fallback | Go          |
| **vLLM**          | GPU inference, large models         | Python/CUDA |
| **Speaches**      | Speech-to-text (Whisper)            | Python      |
| **Piper TTS**     | Text-to-speech (CPU)                | Rust/Python |

## Quick Start

```bash
# Deploy namespace + infra
make deploy-infra

# Deploy Ollama (Phase 1)
make deploy-ollama

# Test
make test-health
```

## Namespace

All services deploy to `llm-infra` namespace on the forge K3s cluster.

## Migration Phases

| Phase | Service                     | Status  |
| ----- | --------------------------- | ------- |
| 1     | Centralized Ollama          | Planned |
| 2     | vLLM + Sleep Mode           | Planned |
| 3     | LiteLLM Gateway             | Planned |
| 4     | TTS/STT Services            | Planned |
| 5     | Decommission per-app Ollama | Planned |

## Project Structure

```
synapse/
├── manifests/
│   ├── infra/          # Namespace, PVCs, ConfigMaps
│   ├── apps/           # Deployments + Services
│   └── monitoring/     # Prometheus rules, ServiceMonitors
├── config/             # LiteLLM config, routing rules
├── scripts/            # Deployment helpers, health checks
├── monitoring/
│   ├── dashboards/     # Grafana JSON dashboards
│   └── alerts/         # Alertmanager rules
└── docs/               # Architecture, runbooks
```

## Research Origin

- Architecture: `research-lab/20_RESEARCH/2026-02-10__llm-infra__centralized-gateway-architecture.md`
- Comparison: `research-lab/20_RESEARCH/2026-02-10__llm-gateway__vllm-vs-ollama-devils-advocate.md`
- Related: `arunlabs-vllm` (standalone vLLM deployment)

## License

Private — ArunLabs
