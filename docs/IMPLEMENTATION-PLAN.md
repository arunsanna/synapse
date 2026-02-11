# Synapse Implementation Plan

**Project**: Synapse — Centralized LLM Inference Gateway
**Location**: `/Users/jarvis_arunlab/research-lab/50_PROJECTS/_incubating/synapse/`
**Cluster**: forge (K3s single-node)
**Hardware**: RTX 5090 32GB VRAM, 196GB RAM, 32-core CPU
**Status**: Pre-implementation (Phase 0)
**Last Updated**: 2026-02-10

---

## 1. Executive Summary

### Goals

Consolidate all LLM inference workloads on forge into a single, unified gateway to:

1. **Eliminate redundancy** — Replace per-app Ollama instances with centralized service
2. **Maximize GPU utilization** — Share RTX 5090 across vLLM (6GB), Ollama (18GB), TTS (12GB)
3. **Simplify integration** — Single OpenAI-compatible endpoint at `synapse.arunlabs.com`
4. **Enable intelligent routing** — LiteLLM routes requests to optimal backend (vLLM for speed, Ollama for variety)
5. **Reduce operational overhead** — One deployment to manage instead of scattered services

### Hardware Constraints

- **VRAM Budget**: 32GB total
  - vLLM: ~6GB (Llama-3.2-8B with 75% utilization)
  - Ollama: ~18GB (2 concurrent 7B-8B models)
  - Coqui TTS: ~12GB (all engines loaded)
  - **Total**: 36GB required (4GB over budget) — **requires time-sharing**
- **RAM Budget**: 196GB total, plan for 80% max (157GB) per DA-5
  - Ollama: 96GB limit (2 models × ~40GB each + overhead)
  - vLLM: ~20GB
  - TTS: ~12GB
  - LiteLLM: ~2GB
  - System overhead: ~27GB
- **CPU**: 32 cores, sufficient headroom

### Phased Approach

Six phases with clear go/no-go gates at each stage:

1. **Phase 1**: Centralized Ollama (CPU-only, validate routing)
2. **Phase 2**: vLLM Integration (GPU validation, Sleep Mode testing)
3. **Phase 3**: LiteLLM Gateway (unified endpoint, routing logic)
4. **Phase 4**: TTS/STT Consolidation (migrate Coqui, GPU time-sharing)
5. **Phase 5**: Monitoring & Optimization (Prometheus, DCGM, dashboards)
6. **Phase 6**: Decommission & Cleanup (remove old services)

---

## 2. Architecture Decisions

### Decision Matrix

| Decision              | Chosen Path                      | Rationale                                                                         | DA Reference   |
| --------------------- | -------------------------------- | --------------------------------------------------------------------------------- | -------------- |
| **Inference Backend** | vLLM primary, Ollama fallback    | vLLM faster for production models (Llama-3.2-8B), Ollama for variety (50+ models) | DA-4, DA-12    |
| **Sleep Mode**        | Do NOT enable until tested       | vLLM Issue #21336 — known crashes on Blackwell GPUs                               | DA-1 (P0)      |
| **STT Engine**        | Coqui TTS (faster-whisper)       | Already deployed, faster than Whisper, drop Whisper from vLLM                     | DA-3 (P1)      |
| **TTS Engine**        | Coqui TTS (XTTS v2)              | Already deployed, skip Speaches/Piper                                             | Existing infra |
| **Ollama GPU**        | CPU-only, RAM cache              | VRAM budget too tight (36GB vs 32GB), rely on 96GB RAM for 2 models               | DA-5, DA-11    |
| **LiteLLM Replicas**  | 2 replicas + 24h restart CronJob | Memory leak mitigation                                                            | DA-2 (P1)      |
| **RAM Budget**        | 80% max (157GB)                  | Safety margin for spikes                                                          | DA-5 (P1)      |
| **Max Ollama Models** | 2 concurrent                     | Fits 80GB budget (2×40GB), down from 3                                            | DA-5, DA-11    |
| **Namespace**         | `llm-infra` for all              | Single namespace for clarity                                                      | DA-6           |
| **Auth**              | Phase 5+ (deferred)              | MVP focuses on functionality, add security later                                  | DA-8 (P2)      |
| **SGLang Benchmark**  | Phase 2 task                     | Validate before committing                                                        | DA-4 (P2)      |
| **Monitoring**        | Phase 5 (Prometheus)             | Critical for VRAM/RAM tracking, but not blocking MVP                              | DA-7           |

### Key Constraints from Existing Infra

1. **vLLM image must be published** — Current `imagePullPolicy: Never` prevents registry migration, must push to `registry.arunlabs.com`
2. **vLLM startup probe** — 30min timeout required for model loading
3. **Flash Attention** — Must use FA2, not FA3 (broken on Blackwell)
4. **Service naming** — Never name service "vllm" (env var conflicts)
5. **TLS** — Use existing `arunlabs-wildcard-tls` cert
6. **Ingress** — Traefik (k3s built-in), HTTPS-only

---

## 3. Phase 1: Centralized Ollama

### Goal

Deploy CPU-only Ollama in `llm-infra` namespace, migrate AI Memory System to use it, validate before adding complexity.

### Files Already Created

- `manifests/infra/namespace.yaml`
- `manifests/infra/ollama-pvc.yaml`
- `manifests/apps/ollama.yaml`
- `scripts/pull-models.sh`
- `Makefile`

### Deployment Steps

```bash
cd /Users/jarvis_arunlab/research-lab/50_PROJECTS/_incubating/synapse

# 1. Deploy namespace and PVC
make deploy-infra

# 2. Deploy Ollama (CPU-only, 2 max models, 96Gi RAM)
kubectl apply -f manifests/apps/ollama.yaml

# 3. Wait for ready
kubectl wait -n llm-infra pod -l app=ollama --for=condition=Ready --timeout=300s

# 4. Pull models (via pod exec, not script yet)
kubectl exec -n llm-infra deploy/ollama -- ollama pull llama3.2:3b
kubectl exec -n llm-infra deploy/ollama -- ollama pull llama3.1:8b

# 5. Validate internal access
kubectl run -n llm-infra curl-test --image=curlimages/curl --rm -it --restart=Never \
  -- curl -s http://ollama:11434/api/tags | jq '.models[].name'
# Expected: llama3.2:3b, llama3.1:8b

# 6. Update AI Memory System
# Edit manifests in ai-memory-system repo to point to:
# http://ollama.llm-infra.svc.cluster.local:11434
# (Replace manual Service+Endpoints at 192.168.0.7)

# 7. Test AI Memory integration
# Run memory_search test query, validate no errors

# 8. Decommission old Ollama
docker stop ollama  # On 192.168.0.7
```

### Success Criteria

- [ ] Ollama pod running, 2 models loaded
- [ ] Internal DNS resolves `ollama.llm-infra.svc.cluster.local`
- [ ] AI Memory System can query Ollama via new endpoint
- [ ] Old Ollama (192.168.0.7) no longer needed
- [ ] RAM usage <96GB (verify with `kubectl top pod`)

### Rollback Plan

```bash
# 1. Restore AI Memory to point to 192.168.0.7
# 2. Restart old Ollama container
docker start ollama
# 3. Delete Synapse Ollama
kubectl delete -f manifests/apps/ollama.yaml
```

### Go/No-Go Criteria for Phase 2

- ✅ AI Memory System functional with new Ollama
- ✅ No DNS resolution issues
- ✅ RAM usage stable <96GB
- ✅ Response latency acceptable (<2s for simple queries)
- ❌ If any failure, rollback and debug before proceeding

---

## 4. Phase 2: vLLM Integration

### Goal

Migrate vLLM from `vllm` namespace to `llm-infra`, push image to registry, validate GPU Sleep Mode safety.

### Critical Validations

1. **Sleep Mode Test** (DA-1 P0) — DO NOT ENABLE until proven stable on RTX 5090
2. **SGLang Benchmark** (DA-4 P2) — Compare performance before committing
3. **Registry Migration** — Must push image to `registry.arunlabs.com`

### Deployment Steps

```bash
# 1. Build and push vLLM image to registry
cd /path/to/vllm-dockerfile  # (Wherever custom vLLM image is built)
docker build -t registry.arunlabs.com/vllm-rtx5090:v0.8.1 .
docker push registry.arunlabs.com/vllm-rtx5090:v0.8.1

# 2. Create vLLM manifest in llm-infra namespace
# (New file: manifests/apps/vllm.yaml)

# 3. Deploy vLLM
kubectl apply -f manifests/apps/vllm.yaml

# 4. Wait for model load (30min timeout)
kubectl wait -n llm-infra pod -l app=vllm-inference --for=condition=Ready --timeout=30m

# 5. Test inference
kubectl run -n llm-infra curl-test --image=curlimages/curl --rm -it --restart=Never \
  -- curl -s http://vllm-inference:8000/v1/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"llama-3.2-8b","prompt":"Hello","max_tokens":10}'

# 6. Benchmark SGLang (optional but recommended)
# Deploy SGLang with same model, run locust benchmark, compare throughput

# 7. Test Sleep Mode (CRITICAL — DA-1)
# Enable --enable-sleep flag, run load test, monitor for crashes
# Watch for segfaults or GPU hangs over 24h period
# If unstable, DISABLE Sleep Mode permanently

# 8. Migrate traffic gradually
# Old vLLM namespace → llm-infra namespace
# Update app manifests to point to vllm-inference.llm-infra.svc.cluster.local:8000

# 9. Decommission old vLLM namespace
kubectl delete namespace vllm
```

### Files to Create

- `manifests/apps/vllm.yaml` — Deployment, Service, GPU allocation, startup probe
- `docs/benchmarks/sglang-vs-vllm.md` — Benchmark results (if conducted)
- `docs/SLEEP-MODE-TEST.md` — Sleep Mode validation results

### Success Criteria

- [ ] vLLM responds to inference requests
- [ ] GPU memory usage ~6GB (verify with `nvidia-smi`)
- [ ] Startup probe succeeds within 30min
- [ ] `imagePullPolicy: IfNotPresent` works (pulls from registry)
- [ ] Sleep Mode test result documented (enable/disable decision made)
- [ ] SGLang benchmark complete (if conducted)

### Rollback Plan

```bash
# 1. Restore old vLLM namespace
kubectl create namespace vllm
kubectl apply -f /path/to/old-vllm-manifests/

# 2. Revert app manifests to point to vllm.vllm.svc.cluster.local
# 3. Delete llm-infra vLLM
kubectl delete -f manifests/apps/vllm.yaml
```

### Go/No-Go Criteria for Phase 3

- ✅ vLLM inference working
- ✅ GPU memory stable at ~6GB
- ✅ Sleep Mode decision documented (enabled or disabled with rationale)
- ✅ No GPU crashes over 24h burn-in
- ❌ If Sleep Mode crashes persist, disable and proceed
- ❌ If total VRAM >32GB, block Phase 4 (cannot add TTS)

---

## 5. Phase 3: LiteLLM Gateway

### Goal

Deploy LiteLLM proxy as unified OpenAI-compatible endpoint, expose at `synapse.arunlabs.com`.

### Deployment Steps

```bash
# 1. Create LiteLLM ConfigMap
kubectl create configmap -n llm-infra litellm-config \
  --from-file=config/litellm-config.yaml

# 2. Deploy LiteLLM (2 replicas for memory leak mitigation)
kubectl apply -f manifests/apps/litellm.yaml

# 3. Deploy Service
kubectl apply -f manifests/apps/litellm-service.yaml

# 4. Deploy Ingress
kubectl apply -f manifests/ingress/synapse-ingress.yaml

# 5. Wait for ready
kubectl wait -n llm-infra pod -l app=litellm --for=condition=Ready --timeout=300s

# 6. Test routing logic
# Test 1: Request llama-3.2-8b (should route to vLLM)
curl https://synapse.arunlabs.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama-3.2-8b","messages":[{"role":"user","content":"Hi"}]}'

# Test 2: Request llama3.1:8b (should route to Ollama)
curl https://synapse.arunlabs.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"Hi"}]}'

# Test 3: Request unknown model (should fallback to Ollama)
curl https://synapse.arunlabs.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:7b","messages":[{"role":"user","content":"Hi"}]}'

# 7. Deploy memory leak mitigation CronJob
kubectl apply -f manifests/apps/litellm-restart-cronjob.yaml
# Restarts LiteLLM pods every 24h to prevent memory leaks

# 8. Update client applications
# Change LLM_BASE_URL to https://synapse.arunlabs.com
# (AI Memory System, Spectre-App, etc.)

# 9. Monitor logs for routing decisions
kubectl logs -n llm-infra -l app=litellm -f
```

### Files to Create

- `manifests/apps/litellm.yaml` — Deployment (2 replicas, ConfigMap mount)
- `manifests/apps/litellm-service.yaml` — ClusterIP Service
- `manifests/ingress/synapse-ingress.yaml` — Traefik IngressRoute with TLS
- `manifests/apps/litellm-restart-cronjob.yaml` — Daily rolling restart for memory leak mitigation
- `config/litellm-config.yaml` — Routing rules (already exists, verify correctness)

### LiteLLM Config Validation

**Routing Logic**:

1. Requests for `llama-3.2-8b` → vLLM (fast path)
2. Requests for models in Ollama → Ollama
3. Unknown models → Ollama (fallback)
4. TTS/STT requests → Coqui TTS (Phase 4)

### Success Criteria

- [ ] HTTPS endpoint accessible at `synapse.arunlabs.com`
- [ ] Requests route to correct backend (vLLM vs Ollama)
- [ ] Fallback logic works (unknown models → Ollama)
- [ ] TLS certificate valid (wildcard cert)
- [ ] 2 LiteLLM replicas running
- [ ] CronJob created (verify with `kubectl get cronjob -n llm-infra`)
- [ ] Client apps successfully migrated

### Rollback Plan

```bash
# 1. Revert client app LLM_BASE_URL to old endpoints
# 2. Delete LiteLLM resources
kubectl delete -f manifests/apps/litellm.yaml
kubectl delete -f manifests/apps/litellm-service.yaml
kubectl delete -f manifests/ingress/synapse-ingress.yaml
kubectl delete -f manifests/apps/litellm-restart-cronjob.yaml
```

### Go/No-Go Criteria for Phase 4

- ✅ All routing tests pass
- ✅ HTTPS endpoint stable over 24h
- ✅ No 503 errors from LiteLLM
- ✅ Memory usage <2GB per replica
- ❌ If memory leak exceeds 4GB/day, reduce restart interval to 12h

---

## 6. Phase 4: TTS/STT Consolidation

### Goal

Migrate Coqui TTS to `llm-infra` namespace, integrate with LiteLLM, validate GPU time-sharing.

### Critical VRAM Budget Decision

**Current Allocation**:

- vLLM: 6GB
- Ollama: 0GB (CPU-only)
- Coqui TTS: 12GB
- **Total**: 18GB (14GB headroom)

**Time-Sharing Strategy**:
Coqui TTS and vLLM will NOT run concurrently. Options:

1. **Manual GPU Scheduling** — Use node taints/affinity to prevent both running simultaneously
2. **On-Demand TTS** — Scale Coqui to 0 replicas when not in use, scale up on-demand
3. **Separate GPU Partition** — Use MIG (not available on consumer RTX 5090)

**Chosen Approach**: On-demand scaling (Option 2) — Most flexible, avoids manual coordination.

### Deployment Steps

```bash
# 1. Copy Coqui TTS manifests to llm-infra
kubectl get deployment -n tts tts-inference -o yaml > manifests/apps/coqui-tts.yaml
# Edit: Change namespace to llm-infra, set replicas=0 by default

# 2. Deploy Coqui TTS (initially scaled to 0)
kubectl apply -f manifests/apps/coqui-tts.yaml

# 3. Test on-demand scaling
kubectl scale -n llm-infra deployment/coqui-tts --replicas=1
kubectl wait -n llm-infra pod -l app=coqui-tts --for=condition=Ready --timeout=300s

# 4. Test STT endpoint
curl http://coqui-tts.llm-infra.svc.cluster.local:8000/v1/audio/transcriptions \
  -F "file=@test.wav" -F "model=faster-whisper"

# 5. Test TTS endpoint
curl http://coqui-tts.llm-infra.svc.cluster.local:8000/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"xtts-v2","input":"Hello world","voice":"default"}'

# 6. Update LiteLLM config to route TTS/STT
# Add routes for /v1/audio/transcriptions → coqui-tts
# Add routes for /v1/audio/speech → coqui-tts

# 7. Expose via Synapse gateway
# Test: curl https://synapse.arunlabs.com/v1/audio/transcriptions ...

# 8. Validate GPU memory
# With vLLM running: nvidia-smi (should show 6GB)
# Scale Coqui to 1: nvidia-smi (should show 18GB) — CONFLICT!
# Scale vLLM to 0: nvidia-smi (should show 12GB) — TTS only

# 9. Create scaling automation script
# scripts/scale-tts.sh — Scales vLLM down, TTS up (and vice versa)

# 10. Decision point: OpenVoice
# Option A: Archive OpenVoice (less mature, redundant)
# Option B: Keep both, route based on quality/speed preference
# Recommendation: Archive for now, revisit if Coqui insufficient

# 11. Decommission old TTS namespace
kubectl delete namespace tts
```

### Files to Create

- `manifests/apps/coqui-tts.yaml` — Deployment, Service, GPU allocation
- `scripts/scale-tts.sh` — Automate vLLM/TTS mutual exclusion
- `docs/TTS-GPU-SHARING.md` — Document time-sharing strategy
- `config/litellm-config.yaml` — Updated with TTS/STT routes

### Success Criteria

- [ ] Coqui TTS responds to STT requests
- [ ] Coqui TTS responds to TTS requests
- [ ] Requests route through Synapse gateway
- [ ] GPU memory does not exceed 32GB (validated with both services)
- [ ] Scaling script works (vLLM↔TTS swap)
- [ ] Decision on OpenVoice documented (archive or keep)

### Rollback Plan

```bash
# 1. Restore old TTS namespace
kubectl create namespace tts
kubectl apply -f /path/to/old-tts-manifests/

# 2. Revert LiteLLM config (remove TTS routes)
# 3. Delete llm-infra Coqui TTS
kubectl delete -f manifests/apps/coqui-tts.yaml
```

### Go/No-Go Criteria for Phase 5

- ✅ TTS/STT functional via Synapse
- ✅ GPU time-sharing validated (no OOM errors)
- ✅ Scaling automation tested
- ❌ If GPU conflicts persist, revert to separate namespace

---

## 7. Phase 5: Monitoring & Optimization

### Goal

Deploy Prometheus + Grafana + DCGM Exporter for GPU metrics, create dashboards, set alerts.

### Deployment Steps

```bash
# 1. Install Prometheus Operator
kubectl apply -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/bundle.yaml

# 2. Deploy DCGM Exporter for GPU metrics
kubectl apply -f manifests/monitoring/dcgm-exporter.yaml

# 3. Deploy Prometheus
kubectl apply -f manifests/monitoring/prometheus.yaml

# 4. Deploy Grafana
kubectl apply -f manifests/monitoring/grafana.yaml

# 5. Create Grafana dashboards
# Import dashboards:
# - LLM Overview (requests/latency/errors per backend)
# - GPU Deep Dive (VRAM/utilization/temperature)
# - Ollama RAM Usage (track memory growth)
# - LiteLLM Health (replica status, memory leak tracking)

# 6. Configure alerts
# - GPU VRAM >30GB (warning)
# - GPU VRAM >32GB (critical)
# - Ollama RAM >90GB (warning)
# - LiteLLM memory >3GB/replica (restart needed)
# - vLLM down >5min (critical)
# - Synapse endpoint 5xx >1% (warning)

# 7. Expose Grafana via Ingress
kubectl apply -f manifests/ingress/grafana-ingress.yaml
# Access at: https://grafana.arunlabs.com

# 8. Set up log aggregation (optional)
# Loki for centralized logs (deferred to future phase)
```

### Files to Create

- `manifests/monitoring/dcgm-exporter.yaml` — DaemonSet for GPU metrics
- `manifests/monitoring/prometheus.yaml` — Prometheus deployment + config
- `manifests/monitoring/grafana.yaml` — Grafana deployment
- `manifests/monitoring/servicemonitor-vllm.yaml` — Scrape vLLM metrics
- `manifests/monitoring/servicemonitor-ollama.yaml` — Scrape Ollama metrics
- `manifests/monitoring/servicemonitor-litellm.yaml` — Scrape LiteLLM metrics
- `manifests/ingress/grafana-ingress.yaml` — HTTPS access to Grafana
- `dashboards/llm-overview.json` — Grafana dashboard JSON
- `dashboards/gpu-deep-dive.json` — GPU metrics dashboard
- `config/prometheus-alerts.yaml` — Alert rules

### Success Criteria

- [ ] Prometheus scraping all targets (vLLM, Ollama, LiteLLM, DCGM)
- [ ] Grafana dashboards show live metrics
- [ ] GPU VRAM visible in real-time
- [ ] Alerts trigger correctly (test by forcing VRAM spike)
- [ ] Historical data retained (7 day retention minimum)

### Rollback Plan

Monitoring is non-critical; rollback = delete monitoring namespace.

```bash
kubectl delete namespace monitoring
```

### Go/No-Go Criteria for Phase 6

- ✅ All metrics visible
- ✅ Alerts tested and working
- ❌ If Prometheus unstable, defer to future iteration (not blocking)

---

## 8. Phase 6: Decommission & Cleanup

### Goal

Remove old namespaces, update all app manifests to use Synapse, final validation.

### Cleanup Steps

```bash
# 1. Identify all apps pointing to old endpoints
# Search for references to:
# - vllm.vllm.svc.cluster.local
# - tts-inference.tts.svc.cluster.local
# - 192.168.0.7:11434 (old Ollama)

# 2. Update app manifests
# Change LLM_BASE_URL to https://synapse.arunlabs.com
# Redeploy affected apps

# 3. Verify no traffic to old services
# Check logs: kubectl logs -n vllm -l app=vllm-inference (should be silent)

# 4. Delete old namespaces
kubectl delete namespace vllm
kubectl delete namespace tts
kubectl delete namespace openvoice  # If archived

# 5. Stop old Ollama container
docker stop ollama  # On 192.168.0.7
docker rm ollama

# 6. Clean up AI Memory manual Service+Endpoints
kubectl delete -n ai-memory service ollama
kubectl delete -n ai-memory endpoints ollama

# 7. Update documentation
# - README.md: Update architecture diagram
# - CHANGELOG.md: Document migration
# - INTEGRATIONS.md: List apps using Synapse

# 8. Final validation
# Run end-to-end test:
# - vLLM inference via Synapse
# - Ollama inference via Synapse
# - TTS via Synapse
# - STT via Synapse

# 9. Backup critical data
# - Ollama models (PVC snapshot)
# - vLLM HF cache (PVC snapshot)
# - LiteLLM config (git commit)

# 10. Declare production-ready
# Update status in INDEX.md: graduated → production
```

### Files to Update

- `README.md` — Architecture diagram, endpoint URLs
- `CHANGELOG.md` — Migration timeline
- `docs/INTEGRATIONS.md` — List of apps using Synapse
- `00_INDEX/NOW.md` — Update project status

### Success Criteria

- [ ] All old namespaces deleted
- [ ] All apps migrated to Synapse
- [ ] No orphaned resources (PVCs, Services, Ingresses)
- [ ] Documentation updated
- [ ] End-to-end tests pass
- [ ] No errors in Synapse logs for 24h

### Final Validation Checklist

```bash
# Test each capability:
curl https://synapse.arunlabs.com/v1/chat/completions \
  -d '{"model":"llama-3.2-8b","messages":[{"role":"user","content":"Test vLLM"}]}'

curl https://synapse.arunlabs.com/v1/chat/completions \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"Test Ollama"}]}'

curl https://synapse.arunlabs.com/v1/audio/transcriptions \
  -F "file=@test.wav" -F "model=faster-whisper"

curl https://synapse.arunlabs.com/v1/audio/speech \
  -d '{"model":"xtts-v2","input":"Test TTS"}'
```

---

## 9. Risk Register

| Risk ID   | Description                         | Likelihood | Impact   | Mitigation                                         | Phase  |
| --------- | ----------------------------------- | ---------- | -------- | -------------------------------------------------- | ------ |
| **DA-1**  | vLLM Sleep Mode crashes on RTX 5090 | High       | Critical | Test in Phase 2, disable if unstable               | 2      |
| **DA-2**  | LiteLLM memory leaks                | High       | Medium   | 2 replicas + 24h restart CronJob                   | 3      |
| **DA-3**  | Whisper in vLLM wastes VRAM         | Medium     | Low      | Use Coqui faster-whisper, drop Whisper             | 2, 4   |
| **DA-4**  | SGLang outperforms vLLM             | Medium     | Medium   | Benchmark in Phase 2, switch if >20% faster        | 2      |
| **DA-5**  | RAM budget exceeded                 | Medium     | High     | Limit Ollama to 2 models (96GB), monitor           | 1      |
| **DA-6**  | Operator overwhelm (6 services)     | Low        | Medium   | Start simple (Ollama-only), add incrementally      | All    |
| **DA-7**  | Hidden operator time cost           | Medium     | Medium   | Track time in each phase, reassess ROI             | 5      |
| **DA-8**  | No authentication                   | Medium     | High     | Add API keys in future phase (post-MVP)            | Future |
| **DA-11** | VRAM budget too tight               | High       | Critical | Validate empirically, use time-sharing for TTS     | 2, 4   |
| **DA-12** | Premature complexity                | Medium     | Medium   | Start Ollama-only, measure before adding           | 1      |
| **R-1**   | vLLM image pull fails               | Low        | Medium   | Test registry push/pull before Phase 2             | 2      |
| **R-2**   | GPU time-sharing conflicts          | Medium     | High     | Scaling automation script, monitoring alerts       | 4      |
| **R-3**   | Ingress routing errors              | Low        | High     | Test routing logic thoroughly, rollback plan ready | 3      |
| **R-4**   | Model download failures             | Medium     | Low      | Pre-pull models in Phase 1, verify checksums       | 1      |
| **R-5**   | Cert expiration                     | Low        | Critical | cert-manager auto-renewal, monitor expiry dates    | All    |
| **R-6**   | PVC capacity limits                 | Low        | Medium   | Monitor usage, expand PVCs before 80% full         | All    |
| **R-7**   | DNS resolution issues               | Low        | High     | Test internal DNS before migrating apps            | 1, 3   |
| **R-8**   | LiteLLM config syntax errors        | Medium     | High     | Validate YAML, test routing before exposing        | 3      |
| **R-9**   | Prometheus storage growth           | Medium     | Low      | 7-day retention, prune old data                    | 5      |
| **R-10**  | Startup probe timeout               | Medium     | Medium   | Increase to 30min for vLLM, monitor load times     | 2      |

---

## 10. Files to Create Per Phase

### Phase 1: Centralized Ollama

- ✅ `manifests/infra/namespace.yaml` (exists)
- ✅ `manifests/infra/ollama-pvc.yaml` (exists)
- ✅ `manifests/apps/ollama.yaml` (exists)
- ✅ `scripts/pull-models.sh` (exists)
- ✅ `Makefile` (exists)
- 🔲 `docs/PHASE1-VALIDATION.md` — Test results, AI Memory migration notes

### Phase 2: vLLM Integration

- 🔲 `manifests/apps/vllm.yaml` — Deployment, Service, GPU, startup probe
- 🔲 `scripts/build-vllm.sh` — Build and push to registry
- 🔲 `docs/benchmarks/sglang-vs-vllm.md` — Benchmark results (optional)
- 🔲 `docs/SLEEP-MODE-TEST.md` — Sleep Mode validation log
- 🔲 `docs/PHASE2-VALIDATION.md` — GPU metrics, inference tests

### Phase 3: LiteLLM Gateway

- 🔲 `manifests/apps/litellm.yaml` — Deployment (2 replicas)
- 🔲 `manifests/apps/litellm-service.yaml` — ClusterIP Service
- 🔲 `manifests/ingress/synapse-ingress.yaml` — Traefik IngressRoute
- 🔲 `manifests/apps/litellm-restart-cronjob.yaml` — Daily restart CronJob
- ✅ `config/litellm-config.yaml` (exists, verify correctness)
- 🔲 `docs/ROUTING-TESTS.md` — Routing validation results
- 🔲 `docs/PHASE3-VALIDATION.md` — End-to-end tests

### Phase 4: TTS/STT Consolidation

- 🔲 `manifests/apps/coqui-tts.yaml` — Deployment, Service, GPU
- 🔲 `scripts/scale-tts.sh` — vLLM/TTS mutual exclusion automation
- 🔲 `docs/TTS-GPU-SHARING.md` — Time-sharing strategy documentation
- 🔲 `config/litellm-config.yaml` — Updated with TTS/STT routes
- 🔲 `docs/OPENVOICE-DECISION.md` — Archive or keep rationale
- 🔲 `docs/PHASE4-VALIDATION.md` — TTS/STT test results

### Phase 5: Monitoring & Optimization

- 🔲 `manifests/monitoring/dcgm-exporter.yaml` — GPU metrics DaemonSet
- 🔲 `manifests/monitoring/prometheus.yaml` — Prometheus deployment
- 🔲 `manifests/monitoring/grafana.yaml` — Grafana deployment
- 🔲 `manifests/monitoring/servicemonitor-vllm.yaml` — vLLM scrape config
- 🔲 `manifests/monitoring/servicemonitor-ollama.yaml` — Ollama scrape config
- 🔲 `manifests/monitoring/servicemonitor-litellm.yaml` — LiteLLM scrape config
- 🔲 `manifests/ingress/grafana-ingress.yaml` — HTTPS Grafana access
- 🔲 `dashboards/llm-overview.json` — Grafana dashboard
- 🔲 `dashboards/gpu-deep-dive.json` — GPU metrics dashboard
- 🔲 `config/prometheus-alerts.yaml` — Alert rules
- 🔲 `docs/PHASE5-VALIDATION.md` — Metrics screenshots, alert tests

### Phase 6: Decommission & Cleanup

- 🔲 `docs/MIGRATION-LOG.md` — Apps migrated, endpoints changed
- 🔲 `docs/INTEGRATIONS.md` — List of apps using Synapse
- 🔲 `CHANGELOG.md` — Migration timeline
- 🔲 `docs/PHASE6-VALIDATION.md` — Final end-to-end tests
- 🔲 Update `README.md` — New architecture diagram
- 🔲 Update `00_INDEX/NOW.md` — Project status change

---

## 11. Dependency Graph

```
Phase 1: Centralized Ollama
  ├─ Deploy namespace (llm-infra)
  ├─ Deploy Ollama PVC
  ├─ Deploy Ollama (CPU-only, 2 models)
  ├─ Pull models
  ├─ Validate internal access
  ├─ Migrate AI Memory System
  └─ Decommission old Ollama (192.168.0.7)
      └─ GO/NO-GO GATE → Phase 2

Phase 2: vLLM Integration
  ├─ Build vLLM image (RTX 5090 compatibility)
  ├─ Push to registry.arunlabs.com
  ├─ Deploy vLLM to llm-infra
  ├─ Test inference
  ├─ (Optional) Benchmark SGLang vs vLLM
  ├─ Test Sleep Mode (CRITICAL)
  │   ├─ If stable → Enable Sleep Mode
  │   └─ If crashes → Disable permanently
  ├─ Migrate traffic from old vllm namespace
  └─ Decommission old vllm namespace
      └─ GO/NO-GO GATE → Phase 3

Phase 3: LiteLLM Gateway
  ├─ Depends on: Phase 1 (Ollama running)
  ├─ Depends on: Phase 2 (vLLM running)
  ├─ Create LiteLLM ConfigMap
  ├─ Deploy LiteLLM (2 replicas)
  ├─ Deploy Service + Ingress
  ├─ Test routing (vLLM vs Ollama)
  ├─ Test fallback logic
  ├─ Deploy restart CronJob
  ├─ Migrate client apps
  └─ Validate HTTPS endpoint
      └─ GO/NO-GO GATE → Phase 4

Phase 4: TTS/STT Consolidation
  ├─ Depends on: Phase 3 (Synapse gateway running)
  ├─ Migrate Coqui TTS to llm-infra
  ├─ Create scaling automation (vLLM ↔ TTS)
  ├─ Update LiteLLM config (TTS/STT routes)
  ├─ Test GPU time-sharing
  ├─ Validate VRAM does not exceed 32GB
  ├─ Decide on OpenVoice (archive or keep)
  └─ Decommission old tts namespace
      └─ GO/NO-GO GATE → Phase 5

Phase 5: Monitoring & Optimization
  ├─ Depends on: Phases 1-4 (all services running)
  ├─ Deploy Prometheus Operator
  ├─ Deploy DCGM Exporter
  ├─ Deploy Prometheus
  ├─ Deploy Grafana
  ├─ Create dashboards
  ├─ Configure alerts
  └─ Validate metrics collection
      └─ GO/NO-GO GATE → Phase 6

Phase 6: Decommission & Cleanup
  ├─ Depends on: Phase 5 (monitoring validated)
  ├─ Update all app manifests
  ├─ Delete old namespaces (vllm, tts, openvoice)
  ├─ Stop old Ollama container
  ├─ Clean up AI Memory manual Service+Endpoints
  ├─ Update documentation
  ├─ Final validation tests
  └─ Declare production-ready
```

---

## 12. Timeline Estimate

| Phase     | Duration        | Parallelizable                              | Dependencies                  |
| --------- | --------------- | ------------------------------------------- | ----------------------------- |
| Phase 1   | 2-4 hours       | No                                          | None                          |
| Phase 2   | 4-8 hours       | Partially (benchmark can run async)         | Phase 1 complete              |
| Phase 3   | 3-6 hours       | No                                          | Phases 1 & 2 complete         |
| Phase 4   | 4-6 hours       | No                                          | Phase 3 complete              |
| Phase 5   | 6-12 hours      | Partially (dashboards can be refined async) | Phases 1-4 complete           |
| Phase 6   | 2-4 hours       | No                                          | Phases 1-5 complete           |
| **Total** | **21-40 hours** | —                                           | Sequential execution required |

**Burn-in Time** (not included): 24-48h per phase for stability validation.

---

## 13. Success Metrics

| Metric                | Target                              | Measurement              |
| --------------------- | ----------------------------------- | ------------------------ |
| **API Response Time** | p95 <500ms (vLLM), <2s (Ollama)     | Prometheus histogram     |
| **GPU Utilization**   | >60% during active inference        | DCGM metrics             |
| **VRAM Usage**        | <30GB sustained                     | DCGM metrics             |
| **RAM Usage**         | <157GB (80% max)                    | `kubectl top pod`        |
| **Uptime**            | >99.5%                              | Prometheus uptime metric |
| **Routing Accuracy**  | 100% (requests hit correct backend) | LiteLLM logs             |
| **TTS Latency**       | <3s for 10s audio clip              | Manual testing           |
| **STT Latency**       | <2s for 10s audio clip              | Manual testing           |
| **Model Load Time**   | <30min (vLLM), <2min (Ollama)       | Startup probe metrics    |
| **Error Rate**        | <0.1% 5xx errors                    | Prometheus counter       |

---

## 14. Post-Implementation Tasks

### Deferred to Future Phases

- **Authentication** — API keys, rate limiting (DA-8)
- **Network Policies** — Restrict pod-to-pod traffic (DA-8)
- **Multi-GPU Support** — If second GPU added
- **Model Quantization** — AWQ/GPTQ for larger models
- **Request Batching** — Optimize vLLM throughput
- **Log Aggregation** — Loki for centralized logs
- **Horizontal Autoscaling** — Scale LiteLLM replicas based on load
- **Cost Tracking** — Per-model inference cost metrics
- **A/B Testing** — Compare model quality (vLLM vs Ollama variants)

### Documentation Updates

- [ ] Update research-lab `00_INDEX/INDEX.md` (Synapse graduated)
- [ ] Update research-lab `00_INDEX/NOW.md` (current focus)
- [ ] Create `50_PROJECTS/_incubating/synapse/README.md` (architecture diagram)
- [ ] Create `50_PROJECTS/_incubating/synapse/CHANGELOG.md` (migration log)
- [ ] Store learnings in AI Memory (key decisions, gotchas)

---

## 15. Rollback Strategy (Emergency)

If complete rollback required at any phase:

```bash
# 1. Restore AI Memory Ollama to 192.168.0.7
docker start ollama  # On external machine

# 2. Restore old vLLM namespace
kubectl create namespace vllm
kubectl apply -f /backup/vllm-manifests/

# 3. Restore old TTS namespace
kubectl create namespace tts
kubectl apply -f /backup/tts-manifests/

# 4. Delete llm-infra namespace
kubectl delete namespace llm-infra

# 5. Revert all app manifests to old endpoints
# (AI Memory, Spectre-App, etc.)

# 6. Verify services respond
curl http://vllm.vllm.svc.cluster.local:8000/health
curl http://tts-inference.tts.svc.cluster.local:8000/health
curl http://192.168.0.7:11434/api/tags

# 7. Document failure reason
echo "Rollback due to: [REASON]" > docs/ROLLBACK-LOG.md
```

---

## 16. Appendix: Key Learnings for AI Memory

Store these after implementation:

```python
# Phase 1 completion:
memory_add(
    content="LEARNING: Synapse Phase 1 (Ollama centralization) completed. AI Memory System now points to ollama.llm-infra.svc.cluster.local:11434. Old Ollama at 192.168.0.7 decommissioned.",
    metadata={"type": "milestone", "project": "synapse", "phase": "1"}
)

# Phase 2 completion (if Sleep Mode tested):
memory_add(
    content="DECISION: vLLM Sleep Mode [ENABLED/DISABLED] on RTX 5090 after 24h burn-in test. [Result summary]. Issue #21336 confirmed/resolved.",
    metadata={"type": "decision", "project": "synapse", "phase": "2", "hardware": "rtx-5090"}
)

# Phase 3 completion:
memory_add(
    content="LEARNING: LiteLLM deployed with 2 replicas + 24h restart CronJob for memory leak mitigation (DA-2). Routing logic validated: llama-3.2-8b → vLLM, others → Ollama.",
    metadata={"type": "learning", "project": "synapse", "phase": "3"}
)

# Phase 4 completion:
memory_add(
    content="DECISION: OpenVoice [ARCHIVED/KEPT]. Coqui TTS consolidated under Synapse with on-demand scaling. GPU time-sharing validated via scripts/scale-tts.sh.",
    metadata={"type": "decision", "project": "synapse", "phase": "4"}
)

# Phase 6 completion:
memory_add(
    content="MILESTONE: Synapse production-ready. All LLM workloads consolidated at synapse.arunlabs.com. Old namespaces (vllm, tts, openvoice) decommissioned. Total implementation time: [X hours].",
    metadata={"type": "milestone", "project": "synapse", "phase": "6", "status": "production"}
)
```

---

## 17. Contact & Escalation

| Issue Type        | Action                        |
| ----------------- | ----------------------------- | -------------------------------------------- |
| **P0 (Critical)** | GPU crash, service down >5min | Stop deployment, rollback, debug offline     |
| **P1 (High)**     | Memory leak, VRAM spike       | Restart affected pod, monitor, adjust limits |
| **P2 (Medium)**   | Routing error, slow response  | Check LiteLLM logs, test backend directly    |
| **P3 (Low)**      | Documentation gap             | Note for future update                       |

**Debug Resources**:

- vLLM logs: `kubectl logs -n llm-infra -l app=vllm-inference`
- Ollama logs: `kubectl logs -n llm-infra -l app=ollama`
- LiteLLM logs: `kubectl logs -n llm-infra -l app=litellm`
- GPU state: `nvidia-smi` (SSH to forge)
- DCGM metrics: `kubectl port-forward -n monitoring svc/dcgm-exporter 9400:9400`, then `curl localhost:9400/metrics`

---

**End of Implementation Plan**
