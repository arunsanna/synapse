<p align="center">
  <img src="docs/synapse-architecture.png" alt="Synapse Architecture" width="700">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <strong>Centralized LLM Inference Gateway for Kubernetes</strong><br>
  Intelligent routing between Ollama, vLLM, and TTS/STT services — one OpenAI-compatible API
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://kubernetes.io"><img src="https://img.shields.io/badge/kubernetes-%3E%3D1.26-326CE5?logo=kubernetes&logoColor=white" alt="Kubernetes"></a>
  <a href="https://github.com/BerriAI/litellm"><img src="https://img.shields.io/badge/gateway-LiteLLM-orange" alt="LiteLLM"></a>
  <a href="https://ollama.com"><img src="https://img.shields.io/badge/backend-Ollama-black" alt="Ollama"></a>
  <a href="https://github.com/vllm-project/vllm"><img src="https://img.shields.io/badge/backend-vLLM-purple" alt="vLLM"></a>
</p>

---

## What is Synapse?

Synapse is a **unified LLM inference gateway** that sits in front of multiple AI backends and provides a single OpenAI-compatible API. It intelligently routes requests to the best backend based on model type, hardware availability, and load.

**The problem:** Running local LLM infrastructure means juggling multiple services — Ollama for CPU inference, vLLM for GPU acceleration, separate TTS/STT servers — each with different APIs, ports, and configurations.

**The solution:** Synapse gives all your applications one endpoint. It handles routing, fallback, load balancing, and model lifecycle behind the scenes.

```
┌────────────────────────────┐
│  Your Applications         │
│  (Agents, Chatbots, Apps)  │
└──────────┬─────────────────┘
           │ OpenAI-compatible API
┌──────────▼─────────────────┐
│  Synapse (LiteLLM Router)  │
│  Routing / Fallback / Logs │
└──┬───────────┬──────────┬──┘
   │           │          │
┌──▼───┐  ┌───▼──┐  ┌────▼───┐
│Ollama│  │ vLLM │  │TTS/STT │
│(CPU) │  │(GPU) │  │Services│
└──────┘  └──────┘  └────────┘
```

## Features

- **Unified API** — One OpenAI-compatible endpoint for all inference backends
- **Intelligent Routing** — Automatically routes to the optimal backend per model
- **Automatic Fallback** — GPU overloaded? Falls back to CPU inference transparently
- **Multi-Backend** — Supports Ollama, vLLM, and any OpenAI-compatible service
- **TTS/STT Support** — Routes speech-to-text and text-to-speech alongside LLM inference
- **Kubernetes Native** — Deploys as standard K8s manifests in a single namespace
- **Observable** — Prometheus metrics, Grafana dashboards, alerting rules included
- **Phased Deployment** — Start with Ollama only, add GPU backends when ready

## Architecture

| Component            | Role                                               | Runs On    |
| -------------------- | -------------------------------------------------- | ---------- |
| **LiteLLM Proxy**    | API gateway, routing, fallback, logging            | CPU        |
| **Ollama**           | CPU inference, embeddings, fallback for GPU models | CPU        |
| **vLLM**             | High-performance GPU inference for large models    | GPU        |
| **TTS/STT Services** | Speech-to-text (Whisper), text-to-speech           | CPU or GPU |

### Routing Logic

| Request Type      | Primary Backend | Fallback     |
| ----------------- | --------------- | ------------ |
| Embeddings        | Ollama (CPU)    | —            |
| Small LLMs (7-8B) | vLLM (GPU)      | Ollama (CPU) |
| Large LLMs (70B+) | vLLM (GPU)      | —            |
| Speech-to-Text    | STT service     | —            |
| Text-to-Speech    | TTS service     | —            |

## Quick Start

### Prerequisites

- Kubernetes cluster (K3s, Kind, Minikube, EKS, GKE, etc.)
- `kubectl` configured with cluster access
- NVIDIA GPU + [device plugin](https://github.com/NVIDIA/k8s-device-plugin) (for vLLM — optional for Phase 1)

### Phase 1: Ollama Only (No GPU Required)

Deploy a centralized Ollama instance with CPU inference:

```bash
# Create namespace and storage
kubectl apply -f manifests/infra/

# Deploy Ollama
kubectl apply -f manifests/apps/ollama.yaml

# Pull models
./scripts/pull-models.sh

# Verify
./scripts/health-check.sh
```

Your applications can now point to `http://ollama.llm-infra.svc.cluster.local:11434` using the standard OpenAI-compatible API.

### Phase 2+: Add GPU Backend & Gateway

See the [Deployment Guide](docs/DEPLOYMENT.md) for the full phased rollout including vLLM, LiteLLM router, and TTS/STT services.

## Configuration

### Ollama (CPU Backend)

Key environment variables in `manifests/apps/ollama.yaml`:

| Variable                   | Default    | Description                                     |
| -------------------------- | ---------- | ----------------------------------------------- |
| `OLLAMA_KEEP_ALIVE`        | `-1`       | How long to keep models loaded (`-1` = forever) |
| `OLLAMA_NUM_PARALLEL`      | `8`        | Concurrent requests per model                   |
| `OLLAMA_MAX_LOADED_MODELS` | `2`        | Max models in memory simultaneously             |
| `OLLAMA_NUM_THREADS`       | `0` (auto) | CPU threads for inference                       |

### LiteLLM Router

Edit `config/litellm-config.yaml` to define your routing rules:

```yaml
model_list:
  # Route embedding requests to Ollama
  - model_name: mxbai-embed-large
    litellm_params:
      model: ollama/mxbai-embed-large
      api_base: http://ollama.llm-infra.svc.cluster.local:11434

  # Route LLM requests to vLLM (primary) with Ollama fallback
  - model_name: llama3.1-8b
    litellm_params:
      model: openai/meta-llama/Meta-Llama-3.1-8B-Instruct
      api_base: http://vllm-inference.llm-infra.svc.cluster.local:8001/v1
      order: 1 # Try GPU first

  - model_name: llama3.1-8b
    litellm_params:
      model: ollama/llama3.1:8b
      api_base: http://ollama.llm-infra.svc.cluster.local:11434
      order: 2 # CPU fallback
```

### Resource Tuning

Adjust resource limits in the manifests to match your hardware:

```yaml
# manifests/apps/ollama.yaml
resources:
  requests:
    memory: 32Gi # Minimum for 1 loaded model
    cpu: 8
  limits:
    memory: 96Gi # Adjust based on available RAM
    cpu: 32 # Adjust based on available cores
```

## Project Structure

```
synapse/
├── manifests/
│   ├── infra/              # Namespace, PVCs, ConfigMaps
│   ├── apps/               # Deployments + Services
│   └── monitoring/         # Prometheus rules, ServiceMonitors
├── config/                 # LiteLLM routing configuration
├── scripts/                # Deployment helpers, health checks
├── monitoring/
│   ├── dashboards/         # Grafana JSON dashboards
│   └── alerts/             # Alertmanager rules
└── docs/                   # Architecture docs, deployment guide
```

## Deployment Phases

Synapse is designed for incremental rollout. Start simple, add complexity when needed.

| Phase | What                                             | GPU Required? | Complexity |
| ----- | ------------------------------------------------ | ------------- | ---------- |
| **1** | Centralized Ollama (CPU inference + embeddings)  | No            | Low        |
| **2** | Add vLLM (GPU inference for large models)        | Yes           | Medium     |
| **3** | Add LiteLLM gateway (unified routing + fallback) | No            | Medium     |
| **4** | Add TTS/STT services (speech workloads)          | Optional      | Medium     |
| **5** | Monitoring stack (Prometheus + Grafana)          | No            | Low        |
| **6** | Decommission per-app instances, consolidate      | No            | Low        |

## Design Principles

1. **Start simple** — Ollama alone covers 80% of use cases. Only add vLLM/LiteLLM when you need more performance.
2. **CPU-first fallback** — Every GPU model should have a CPU fallback path. GPU failures shouldn't break your apps.
3. **One namespace** — All inference services live in `llm-infra` for simplified operations.
4. **OpenAI-compatible** — All backends expose the OpenAI API format. Swap backends without changing application code.
5. **Observable** — If you can't measure it, you can't manage it. Metrics, dashboards, and alerts are first-class.

## Monitoring

Synapse includes pre-built monitoring for:

- **LiteLLM**: Request rates, latency percentiles, fallback events, backend health
- **vLLM**: Tokens/second, TTFT, queue depth, model state (hot/warm/cold)
- **GPU**: VRAM usage, utilization %, temperature, power draw (via DCGM Exporter)
- **Ollama**: Active models, CPU usage, memory consumption

See `monitoring/` for Grafana dashboards and Prometheus alert rules.

## FAQ

**Q: Do I need a GPU?**
No. Phase 1 runs entirely on CPU with Ollama. GPU is only needed for vLLM (Phase 2+).

**Q: Can I use this without Kubernetes?**
The manifests are K8s-native, but the architecture works with Docker Compose too (contributions welcome).

**Q: What models are supported?**
Any model supported by Ollama or vLLM. Ollama supports GGUF models, vLLM supports native HuggingFace models.

**Q: How does GPU sharing work?**
vLLM Sleep Mode offloads model weights to CPU RAM when idle, freeing VRAM for other services. LiteLLM coordinates which backend handles each request.

**Q: What about security?**
For production, add Kubernetes NetworkPolicies (only the gateway should reach backends) and API key authentication on LiteLLM. See the [Deployment Guide](docs/DEPLOYMENT.md).

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Areas where help is especially appreciated:

- Helm chart for parameterized deployment
- Docker Compose alternative
- Additional backend integrations (SGLang, TensorRT-LLM)
- Grafana dashboard improvements
- Documentation and examples

## License

[MIT](LICENSE)
