# Synapse Build Plan

> Centralized LLM Inference Gateway for ArunLabs Forge K3s Cluster

---

## Table of Contents

1. [Current State Assessment](#current-state-assessment)
2. [Resource Budget](#resource-budget)
3. [Phase-by-Phase Build Plan](#phase-by-phase-build-plan)
4. [Consumer Migration Plan](#consumer-migration-plan)
5. [Risk Register](#risk-register)
6. [File Inventory Summary](#file-inventory-summary)

---

## Current State Assessment

### What Exists (Scaffolded)

| File                              | Status | Notes                                                    |
| --------------------------------- | ------ | -------------------------------------------------------- |
| `manifests/infra/namespace.yaml`  | Ready  | `llm-infra` namespace                                    |
| `manifests/infra/ollama-pvc.yaml` | Ready  | 200Gi PVC, local-path                                    |
| `manifests/apps/ollama.yaml`      | Ready  | CPU-only, 32Gi req / 96Gi limit, CUDA_VISIBLE_DEVICES="" |
| `config/litellm-config.yaml`      | Ready  | Full routing config (all phases)                         |
| `scripts/health-check.sh`         | Ready  | Checks namespace, Ollama, LiteLLM                        |
| `scripts/pull-models.sh`          | Ready  | Pulls mxbai-embed-large + llama3.1:8b                    |
| `Makefile`                        | Ready  | deploy-infra, deploy-ollama, deploy-phase1, etc.         |
| `docs/DEPLOYMENT.md`              | Ready  | Example manifests in docs (not deployable files)         |
| `monitoring/alerts/`              | Empty  | Directory exists, no files                               |
| `monitoring/dashboards/`          | Empty  | Directory exists, no files                               |

### What Needs Building

| File                                          | Phase | Priority |
| --------------------------------------------- | ----- | -------- |
| `manifests/apps/litellm.yaml`                 | 3     | High     |
| `manifests/apps/litellm-configmap.yaml`       | 3     | High     |
| `manifests/apps/litellm-rbac.yaml`            | 3     | High     |
| `manifests/apps/litellm-restart-cronjob.yaml` | 3     | High     |
| `manifests/apps/vllm.yaml`                    | 2     | Medium   |
| `manifests/apps/speaches.yaml`                | 4     | Low      |
| `manifests/apps/piper-tts.yaml`               | 4     | Low      |
| `manifests/infra/networkpolicies.yaml`        | 5     | Medium   |
| `manifests/infra/ingress.yaml`                | 5     | Medium   |
| `monitoring/alerts/synapse-alerts.yaml`       | 5     | Low      |
| `monitoring/dashboards/synapse-overview.json` | 5     | Low      |
| `scripts/benchmark.sh`                        | 2     | Medium   |
| `scripts/migrate-consumer.sh`                 | 1     | High     |

### Known Consumers (Must Migrate)

| Consumer                               | Current Endpoint                       | Namespace       | What It Uses                   |
| -------------------------------------- | -------------------------------------- | --------------- | ------------------------------ |
| ai-memory-system (indexer)             | `http://ollama:11434` (same-namespace) | `jarvis-cortex` | Embeddings (mxbai-embed-large) |
| ai-memory-system (extractor)           | `http://ollama:11434` (same-namespace) | `jarvis-cortex` | Embeddings + LLM               |
| ai-memory-system (query/global_search) | `http://ollama:11434` (same-namespace) | `jarvis-cortex` | Embeddings                     |
| ai-memory-system (graph_builder)       | `http://ollama:11434` (same-namespace) | `jarvis-cortex` | Embeddings + LLM               |

**Critical finding**: ai-memory-system has its **own** Ollama deployment in `jarvis-cortex` namespace with:

- 50Gi PVC (vs. Synapse's 200Gi)
- 4Gi memory limit (vs. Synapse's 96Gi)
- **GPU access** (`nvidia.com/gpu: 1`) — Synapse Ollama is CPU-only
- Uses `http://ollama:11434` (same-namespace short name)

This means ai-memory-system's Ollama is **GPU-accelerated** while Synapse's Ollama is intentionally CPU-only. The migration must account for this regression in embedding performance.

---

## Resource Budget

### Hardware: forge (single-node K3s)

| Resource | Total                    | 80% Budget | Reserved for K3s/OS |
| -------- | ------------------------ | ---------- | ------------------- |
| RAM      | 196 GB                   | 157 GB     | 39 GB               |
| CPU      | 64 cores (assumed)       | 51 cores   | 13 cores            |
| GPU      | 1x RTX 5090 (32 GB VRAM) | 1 GPU      | —                   |
| Disk     | 200Gi PVC (Ollama)       | 200 Gi     | —                   |

### Per-Service RAM Budget

| Service               | Requests       | Limits         | GPU             | Phase | Notes                            |
| --------------------- | -------------- | -------------- | --------------- | ----- | -------------------------------- |
| Ollama (CPU)          | 32 Gi          | 96 Gi          | None            | 1     | 2 models loaded (embed + 8B LLM) |
| vLLM                  | 32 Gi          | 48 Gi          | 1 (32GB VRAM)   | 2     | 8B model, 85% GPU util           |
| LiteLLM (x2 replicas) | 2 Gi x2 = 4 Gi | 4 Gi x2 = 8 Gi | None            | 3     | Memory leak mitigation           |
| Speaches STT          | 8 Gi           | 16 Gi          | None (CPU mode) | 4     | faster-whisper large-v3          |
| Piper TTS             | 1 Gi           | 4 Gi           | None            | 4     | Lightweight CPU                  |
| **Total Requests**    | **77 Gi**      | —              | —               | —     |                                  |
| **Total Limits**      | —              | **172 Gi**     | —               | —     | **Exceeds 157Gi budget!**        |

### Budget Problem

The sum of limits (172 Gi) exceeds the 157 Gi budget. K8s limits are burst ceilings, not reservations, so this is acceptable on a single-node cluster where not all services will hit limits simultaneously. However, the risk is OOMKill under peak load.

**Mitigation options (pick one):**

1. **Reduce Ollama limit to 64 Gi** (load 1 model at a time instead of 2) → total limits = 140 Gi ✓
2. **Run Speaches with GPU** instead of CPU (uses VRAM not RAM, drops to 4Gi RAM) → total limits = 160 Gi ≈ OK
3. **Accept overcommit** and rely on OOMKill priority (Ollama recovers gracefully from OOMKill)

**Recommendation**: Option 1. Reduce `OLLAMA_MAX_LOADED_MODELS` to 1 and limit to 64Gi. The embedding model (~670MB) is tiny; the 8B LLM on CPU is the heavy one. Since vLLM will handle LLM inference in Phase 2+, Ollama only needs to keep the embedding model loaded.

### Revised Budget (Option 1)

| Service      | Requests  | Limits     | Phase |
| ------------ | --------- | ---------- | ----- |
| Ollama (CPU) | 16 Gi     | 64 Gi      | 1     |
| vLLM         | 32 Gi     | 48 Gi      | 2     |
| LiteLLM (x2) | 4 Gi      | 8 Gi       | 3     |
| Speaches STT | 8 Gi      | 16 Gi      | 4     |
| Piper TTS    | 1 Gi      | 4 Gi       | 4     |
| **Total**    | **61 Gi** | **140 Gi** | —     |

This leaves 17 Gi headroom under the 157 Gi budget for limits.

---

## Phase-by-Phase Build Plan

### Phase 1: Centralized Ollama (CPU Only)

**Goal**: Single centralized Ollama serving embeddings to all consumers. Prove the "shared inference" pattern works before adding complexity.

**Duration target**: Deploy and verify in one session.

#### 1.1 Files to Create/Modify

| Action | File                          | Description                                |
| ------ | ----------------------------- | ------------------------------------------ |
| Modify | `manifests/apps/ollama.yaml`  | Reduce limits to 64Gi, MAX_LOADED_MODELS=1 |
| Create | `scripts/migrate-consumer.sh` | Script to update consumer configmaps       |

#### 1.2 Changes to `manifests/apps/ollama.yaml`

```yaml
# Change these values:
OLLAMA_MAX_LOADED_MODELS: "1" # was "2"

resources:
  requests:
    memory: 16Gi # was 32Gi
    cpu: 4 # was 8 (embeddings don't need 8 cores)
  limits:
    memory: 64Gi # was 96Gi
    cpu: 16 # was 32
```

#### 1.3 Build Sequence

```bash
# Step 1: Deploy infrastructure
ssh forge
cd /path/to/synapse  # or apply from megamind via kubectl context
sudo kubectl apply -f manifests/infra/namespace.yaml
sudo kubectl apply -f manifests/infra/ollama-pvc.yaml

# Step 2: Verify PVC is bound
sudo kubectl get pvc -n llm-infra
# Expected: ollama-models   Bound   local-path   200Gi

# Step 3: Deploy Ollama
sudo kubectl apply -f manifests/apps/ollama.yaml

# Step 4: Wait for ready
sudo kubectl -n llm-infra wait --for=condition=ready pod -l app=ollama --timeout=300s

# Step 5: Pull models
# Run from megamind (kubectl context set to forge)
./scripts/pull-models.sh

# Step 6: Verify
./scripts/health-check.sh
```

#### 1.4 Success Criteria

| Criterion                  | How to Test                                                                           | Pass/Fail       |
| -------------------------- | ------------------------------------------------------------------------------------- | --------------- |
| Ollama pod Running + Ready | `kubectl -n llm-infra get pods`                                                       | Pod 1/1 Running |
| PVC bound                  | `kubectl -n llm-infra get pvc`                                                        | Bound           |
| mxbai-embed-large loaded   | `kubectl exec ollama-xxx -- ollama list`                                              | Model present   |
| Embedding request works    | `curl ollama:11434/api/embeddings -d '{"model":"mxbai-embed-large","prompt":"test"}'` | 200 + vector    |
| Embedding latency < 100ms  | Time the curl above                                                                   | < 100ms         |
| LLM chat works (CPU)       | `curl ollama:11434/v1/chat/completions` with llama3.1:8b                              | 200 + response  |
| Memory < 64Gi              | `kubectl top pod -n llm-infra`                                                        | < 64Gi          |

#### 1.5 Phase 1 Blockers

- **None**. Phase 1 is self-contained. No dependency on GPU, external images, or other services.

---

### Phase 2: vLLM GPU Inference

**Goal**: Add GPU-accelerated inference for LLM chat/completion. Ollama remains as CPU fallback and embedding backend.

**Prerequisite**: NVIDIA device plugin installed on forge, RTX 5090 visible to K3s.

#### 2.1 Pre-Flight Checks

```bash
# Verify GPU is visible to K3s
sudo kubectl get nodes -o json | jq '.items[].status.capacity["nvidia.com/gpu"]'
# Expected: "1"

# Verify NVIDIA device plugin
sudo kubectl get pods -n kube-system | grep nvidia
# Expected: nvidia-device-plugin-xxx  Running

# Verify CUDA on RTX 5090
sudo kubectl run gpu-test --image=nvidia/cuda:12.6.0-base-ubuntu24.04 --rm -it --restart=Never --limits='nvidia.com/gpu=1' -- nvidia-smi
# Expected: RTX 5090, driver version, CUDA version
```

#### 2.2 Files to Create

| Action | File                            | Description                     |
| ------ | ------------------------------- | ------------------------------- |
| Create | `manifests/apps/vllm.yaml`      | vLLM Deployment + Service       |
| Create | `manifests/infra/vllm-pvc.yaml` | PVC for HuggingFace model cache |
| Create | `scripts/benchmark.sh`          | Benchmark vLLM throughput       |

#### 2.3 Key Design Decisions for vLLM Manifest

```yaml
# Critical: vLLM on RTX 5090 (Blackwell) considerations
# 1. Service name MUST NOT be "vllm" (env var collision)
# 2. Sleep mode is BUGGY on Blackwell — DO NOT enable
# 3. Use --enforce-eager if tensor parallelism issues occur
# 4. Pin to specific vLLM version (not :latest) for reproducibility

apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-inference
  namespace: llm-infra
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:v0.7.2 # Pin version
          args:
            - --model=meta-llama/Meta-Llama-3.1-8B-Instruct
            - --gpu-memory-utilization=0.85
            - --max-model-len=8192
            - --disable-log-requests # Reduce I/O
            - --enforce-eager # Avoid CUDA graph issues on Blackwell
          env:
            - name: HF_HOME
              value: /models
            - name: HUGGING_FACE_HUB_TOKEN
              valueFrom:
                secretKeyRef:
                  name: hf-token
                  key: token
                  optional: true
          resources:
            limits:
              nvidia.com/gpu: 1
              memory: 48Gi
            requests:
              nvidia.com/gpu: 1
              memory: 32Gi
          volumeMounts:
            - name: model-cache
              mountPath: /models
          ports:
            - containerPort: 8000
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            failureThreshold: 60 # 30 min startup budget
            periodSeconds: 30
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 30
            timeoutSeconds: 10
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
      volumes:
        - name: model-cache
          persistentVolumeClaim:
            claimName: vllm-model-cache
---
# Service on port 8001 (matches litellm-config.yaml routing)
apiVersion: v1
kind: Service
metadata:
  name: vllm-inference
  namespace: llm-infra
spec:
  selector:
    app: vllm-inference
  ports:
    - port: 8001
      targetPort: 8000
  type: ClusterIP
```

#### 2.4 HuggingFace Token Secret

```bash
# Create HF token secret for gated models (Llama)
sudo kubectl create secret generic hf-token \
  --from-literal=token=hf_xxxxx \
  -n llm-infra
```

#### 2.5 vLLM PVC

```yaml
# manifests/infra/vllm-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: vllm-model-cache
  namespace: llm-infra
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 100Gi # 8B model ≈ 16GB, 70B ≈ 40GB+
  storageClassName: local-path
```

#### 2.6 Build Sequence

```bash
# Step 1: Create HF secret (if using gated models)
sudo kubectl create secret generic hf-token --from-literal=token=hf_xxxxx -n llm-infra

# Step 2: Deploy PVC
sudo kubectl apply -f manifests/infra/vllm-pvc.yaml

# Step 3: Deploy vLLM
sudo kubectl apply -f manifests/apps/vllm.yaml

# Step 4: Watch startup (model download takes time)
sudo kubectl -n llm-infra logs -f deploy/vllm-inference

# Step 5: Wait for ready (up to 30 min for first model download)
sudo kubectl -n llm-infra wait --for=condition=ready pod -l app=vllm-inference --timeout=1800s

# Step 6: Benchmark
./scripts/benchmark.sh
```

#### 2.7 Success Criteria

| Criterion                | How to Test                                             | Pass/Fail       |
| ------------------------ | ------------------------------------------------------- | --------------- |
| vLLM pod Running + Ready | `kubectl -n llm-infra get pods`                         | Pod 1/1 Running |
| GPU allocated            | `kubectl describe pod vllm-xxx` shows nvidia.com/gpu: 1 | Allocated       |
| Chat completion works    | `curl vllm-inference:8001/v1/chat/completions`          | 200 + response  |
| Tokens/sec > 100 (8B)    | `scripts/benchmark.sh`                                  | > 100 tok/s     |
| GPU VRAM < 28GB (85%)    | `nvidia-smi` on forge                                   | < 28GB          |
| Model survives restart   | Delete pod, wait for new one, model loads from PVC      | < 5 min restart |

#### 2.8 Phase 2 Risks

- **RTX 5090 CUDA compatibility**: vLLM may not fully support Blackwell arch. Mitigation: use `--enforce-eager` flag, pin to latest vLLM version known to work.
- **GPU contention**: ai-memory-system's Ollama also claims `nvidia.com/gpu: 1`. Both cannot run simultaneously on a single GPU. **This is a hard blocker** — see Migration Plan.
- **Model download time**: First pull of Meta-Llama-3.1-8B-Instruct is ~16GB. Ensure PVC has space.

---

### Phase 3: LiteLLM Gateway

**Goal**: Deploy the unified routing proxy so all consumers use a single OpenAI-compatible endpoint. Enables model routing, fallback, and future model swaps without consumer changes.

#### 3.1 Files to Create

| Action | File                                          | Description                                     |
| ------ | --------------------------------------------- | ----------------------------------------------- |
| Create | `manifests/apps/litellm-configmap.yaml`       | ConfigMap wrapping `config/litellm-config.yaml` |
| Create | `manifests/apps/litellm.yaml`                 | Deployment (2 replicas) + Service               |
| Create | `manifests/apps/litellm-rbac.yaml`            | ServiceAccount + Role for CronJob               |
| Create | `manifests/apps/litellm-restart-cronjob.yaml` | Daily rolling restart at 4 AM                   |
| Modify | `Makefile`                                    | Add `deploy-litellm`, `deploy-phase3` targets   |

#### 3.2 ConfigMap

```yaml
# manifests/apps/litellm-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: litellm-config
  namespace: llm-infra
  labels:
    app.kubernetes.io/part-of: synapse
data:
  config.yaml: |
    # Content of config/litellm-config.yaml goes here
    # Or use: kubectl create configmap litellm-config --from-file=config.yaml=config/litellm-config.yaml
```

Note: The `config/litellm-config.yaml` should be deployed phase-appropriately:

- Phase 3 (initial): Only Ollama + vLLM routes
- Phase 4: Add Speaches + Piper routes
- Create a `config/litellm-config-phase3.yaml` variant or use kustomize overlays

#### 3.3 LiteLLM RBAC (for CronJob restart)

```yaml
# manifests/apps/litellm-rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: litellm-restart-sa
  namespace: llm-infra
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: litellm-restart-role
  namespace: llm-infra
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    resourceNames: ["litellm-proxy"]
    verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: litellm-restart-binding
  namespace: llm-infra
subjects:
  - kind: ServiceAccount
    name: litellm-restart-sa
roleRef:
  kind: Role
  name: litellm-restart-role
  apiGroup: rbac.authorization.k8s.io
```

#### 3.4 LiteLLM Deployment

Key points vs. the DEPLOYMENT.md example:

- Pin LiteLLM image version (not `:latest`)
- Add `LITELLM_LOG_LEVEL=WARNING` to reduce log noise
- 2 replicas for HA during rolling restart
- Resource requests/limits per the budget table above

#### 3.5 CronJob for Memory Leak Mitigation

```yaml
# manifests/apps/litellm-restart-cronjob.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: litellm-restart
  namespace: llm-infra
spec:
  schedule: "0 4 * * *" # Daily at 4 AM UTC
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: litellm-restart-sa
          containers:
            - name: restart
              image: bitnami/kubectl:1.29
              command:
                - kubectl
                - rollout
                - restart
                - deployment/litellm-proxy
                - -n
                - llm-infra
          restartPolicy: OnFailure
```

#### 3.6 Build Sequence

```bash
# Step 1: Create ConfigMap from config file
sudo kubectl create configmap litellm-config \
  --from-file=config.yaml=config/litellm-config.yaml \
  -n llm-infra --dry-run=client -o yaml | sudo kubectl apply -f -

# Step 2: Deploy RBAC
sudo kubectl apply -f manifests/apps/litellm-rbac.yaml

# Step 3: Deploy LiteLLM
sudo kubectl apply -f manifests/apps/litellm.yaml

# Step 4: Wait for both replicas
sudo kubectl -n llm-infra wait --for=condition=ready pod -l app=litellm-proxy --timeout=120s

# Step 5: Deploy CronJob
sudo kubectl apply -f manifests/apps/litellm-restart-cronjob.yaml

# Step 6: Test routing
curl http://litellm-proxy.llm-infra.svc.cluster.local:8000/health
curl http://litellm-proxy.llm-infra.svc.cluster.local:8000/v1/models
```

#### 3.7 Success Criteria

| Criterion                           | How to Test                                                    | Pass/Fail               |
| ----------------------------------- | -------------------------------------------------------------- | ----------------------- |
| 2 LiteLLM pods Running              | `kubectl -n llm-infra get pods -l app=litellm-proxy`           | 2/2 Running             |
| `/health` returns OK                | `curl litellm-proxy:8000/health`                               | 200                     |
| `/v1/models` lists routes           | `curl litellm-proxy:8000/v1/models`                            | JSON with model list    |
| Embedding via gateway               | `curl litellm-proxy:8000/v1/embeddings` with mxbai-embed-large | 200 + vector            |
| LLM via gateway → vLLM              | `curl litellm-proxy:8000/v1/chat/completions` with llama3.1-8b | 200 + response          |
| Fallback: kill vLLM, retry → Ollama | Scale vLLM to 0, request llama3.1-8b                           | Falls back to Ollama    |
| CronJob scheduled                   | `kubectl get cronjob -n llm-infra`                             | SCHEDULE = 0 4 \* \* \* |
| Routing overhead < 20ms             | Compare direct Ollama vs. via LiteLLM                          | Delta < 20ms            |

---

### Phase 4: TTS/STT Services

**Goal**: Add speech services behind the gateway.

#### 4.1 Files to Create

| Action | File                            | Description                             |
| ------ | ------------------------------- | --------------------------------------- |
| Create | `manifests/apps/speaches.yaml`  | Speaches Deployment + Service           |
| Create | `manifests/apps/piper-tts.yaml` | Piper TTS Deployment + Service          |
| Modify | `config/litellm-config.yaml`    | Verify STT/TTS routes (already present) |
| Modify | `scripts/health-check.sh`       | Add Speaches + Piper health checks      |

#### 4.2 Speaches (faster-whisper STT)

Decision: Run Speaches in **CPU mode** initially. GPU mode would contend with vLLM for the single RTX 5090. STT is bursty (not always running), so CPU is acceptable for initial deployment.

```yaml
# Key differences from DEPLOYMENT.md example:
resources:
  requests:
    memory: 8Gi
    cpu: 4
  limits:
    memory: 16Gi
    cpu: 8
env:
  - name: WHISPER_MODEL
    value: "large-v3"
  - name: CUDA_VISIBLE_DEVICES
    value: "" # Force CPU mode
```

#### 4.3 Piper TTS

Lightweight CPU service. The DEPLOYMENT.md example is close to final. Adjust:

```yaml
resources:
  requests:
    memory: 1Gi
    cpu: 1
  limits:
    memory: 4Gi
    cpu: 4
```

#### 4.4 Build Sequence

```bash
# Step 1: Deploy Speaches
sudo kubectl apply -f manifests/apps/speaches.yaml
sudo kubectl -n llm-infra wait --for=condition=ready pod -l app=speaches-stt --timeout=300s

# Step 2: Deploy Piper
sudo kubectl apply -f manifests/apps/piper-tts.yaml
sudo kubectl -n llm-infra wait --for=condition=ready pod -l app=piper-tts --timeout=120s

# Step 3: Update ConfigMap if routes changed
sudo kubectl create configmap litellm-config \
  --from-file=config.yaml=config/litellm-config.yaml \
  -n llm-infra --dry-run=client -o yaml | sudo kubectl apply -f -
sudo kubectl -n llm-infra rollout restart deployment/litellm-proxy

# Step 4: Test
curl litellm-proxy:8000/v1/audio/transcriptions -F file=@test.wav -F model=whisper-large-v3
curl litellm-proxy:8000/v1/audio/speech -d '{"model":"piper-tts","input":"Hello world"}'
```

#### 4.5 Success Criteria

| Criterion            | How to Test                                         | Pass/Fail     |
| -------------------- | --------------------------------------------------- | ------------- |
| Speaches pod Running | `kubectl -n llm-infra get pods -l app=speaches-stt` | 1/1 Running   |
| Piper pod Running    | `kubectl -n llm-infra get pods -l app=piper-tts`    | 1/1 Running   |
| STT via gateway      | POST audio file to `/v1/audio/transcriptions`       | 200 + text    |
| TTS via gateway      | POST text to `/v1/audio/speech`                     | 200 + audio   |
| Memory within budget | `kubectl top pods -n llm-infra`                     | Total < 140Gi |

---

### Phase 5: Security + Monitoring

**Goal**: Lock down network access, add observability, configure ingress.

#### 5.1 Files to Create

| Action | File                                          | Description                                      |
| ------ | --------------------------------------------- | ------------------------------------------------ |
| Create | `manifests/infra/networkpolicies.yaml`        | Restrict pod-to-pod traffic                      |
| Create | `manifests/infra/ingress.yaml`                | External access via Traefik                      |
| Create | `monitoring/alerts/synapse-alerts.yaml`       | PrometheusRule for alerts                        |
| Create | `monitoring/dashboards/synapse-overview.json` | Grafana dashboard                                |
| Modify | `Makefile`                                    | Add `deploy-monitoring`, `deploy-phase5` targets |

#### 5.2 NetworkPolicies

```yaml
# Default deny all ingress in llm-infra namespace
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: llm-infra
spec:
  podSelector: {}
  policyTypes:
    - Ingress

---
# Allow LiteLLM to receive traffic from any namespace
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-litellm-ingress
  namespace: llm-infra
spec:
  podSelector:
    matchLabels:
      app: litellm-proxy
  ingress:
    - {} # Allow from any namespace (consumers)
  policyTypes:
    - Ingress

---
# Allow backends to receive traffic only from LiteLLM
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-backend-from-litellm
  namespace: llm-infra
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/part-of: synapse
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: litellm-proxy
  policyTypes:
    - Ingress
```

**Warning**: Default-deny will break direct Ollama access. Consumers must migrate to LiteLLM before enabling NetworkPolicies. Deploy NetworkPolicies LAST, after all consumers are migrated.

#### 5.3 Ingress (External Access)

```yaml
# manifests/infra/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: synapse-gateway
  namespace: llm-infra
  annotations:
    traefik.ingress.kubernetes.io/router.tls: "true"
spec:
  rules:
    - host: llm.arunlabs.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: litellm-proxy
                port:
                  number: 8000
  tls:
    - hosts:
        - llm.arunlabs.com
      secretName: llm-tls-cert
```

TLS cert generation:

```bash
AWS_PROFILE=arunlab /Users/megamind/miniconda3/bin/certbot certonly \
  --dns-route53 -d llm.arunlabs.com \
  --config-dir ~/.certbot --work-dir ~/.certbot/work --logs-dir ~/.certbot/logs
```

#### 5.4 Monitoring Alerts

```yaml
# monitoring/alerts/synapse-alerts.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: synapse-alerts
  namespace: llm-infra
spec:
  groups:
    - name: synapse.rules
      rules:
        - alert: OllamaDown
          expr: up{job="ollama"} == 0
          for: 2m
          labels:
            severity: critical
          annotations:
            summary: "Ollama is down"
        - alert: LiteLLMHighMemory
          expr: container_memory_usage_bytes{namespace="llm-infra",pod=~"litellm.*"} > 3.5e9
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "LiteLLM memory usage > 3.5GB (memory leak likely)"
        - alert: VLLMQueueDepth
          expr: vllm:num_requests_waiting > 10
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "vLLM queue depth > 10 (GPU overloaded)"
        - alert: GPUTemperatureHigh
          expr: DCGM_FI_DEV_GPU_TEMP > 85
          for: 5m
          labels:
            severity: critical
          annotations:
            summary: "GPU temperature > 85C"
```

#### 5.5 Success Criteria

| Criterion                             | How to Test                                      | Pass/Fail                 |
| ------------------------------------- | ------------------------------------------------ | ------------------------- |
| NetworkPolicies active                | `kubectl get networkpolicy -n llm-infra`         | 3 policies                |
| Direct Ollama blocked from outside ns | `curl ollama:11434` from non-llm-infra pod       | Connection refused        |
| LiteLLM accessible from any ns        | `curl litellm-proxy:8000` from jarvis-cortex pod | 200                       |
| Ingress works                         | `curl https://llm.arunlabs.com/health`           | 200                       |
| Alerts firing correctly               | Trigger OllamaDown by scaling to 0               | Alert fires in Prometheus |

---

## Consumer Migration Plan

### The Problem

ai-memory-system currently runs its own Ollama in `jarvis-cortex` namespace. It references `http://ollama:11434` (same-namespace DNS). The Synapse Ollama lives in `llm-infra`. We need to migrate without downtime.

### Migration Strategy: Cross-Namespace Service + Gradual Cutover

#### Step 1: Verify Synapse Ollama has the same models

```bash
# Check what models ai-memory-system's Ollama has
sudo kubectl -n jarvis-cortex exec deploy/ollama -- ollama list

# Pull the same models in Synapse's Ollama
./scripts/pull-models.sh mxbai-embed-large  # and any others
```

#### Step 2: Create ExternalName Service in jarvis-cortex

This creates a DNS alias so `ollama.jarvis-cortex.svc` resolves to `ollama.llm-infra.svc`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: ollama-synapse
  namespace: jarvis-cortex
spec:
  type: ExternalName
  externalName: ollama.llm-infra.svc.cluster.local
  ports:
    - port: 11434
```

#### Step 3: Update ai-memory-system ConfigMap

```yaml
# In ai-memory-system's configmap:
# Change FROM:
OLLAMA_BASE_URL: "http://ollama:11434"
OLLAMA_URL: "http://ollama:11434"

# Change TO:
OLLAMA_BASE_URL: "http://ollama.llm-infra.svc.cluster.local:11434"
OLLAMA_URL: "http://ollama.llm-infra.svc.cluster.local:11434"
```

Then rolling restart ai-memory-system workers:

```bash
sudo kubectl -n jarvis-cortex rollout restart deployment/indexer
sudo kubectl -n jarvis-cortex rollout restart deployment/extractor
# etc.
```

#### Step 4: Verify embeddings work cross-namespace

```bash
# From a jarvis-cortex pod, test Synapse's Ollama
sudo kubectl -n jarvis-cortex exec deploy/worker -- \
  curl -s http://ollama.llm-infra.svc.cluster.local:11434/api/embeddings \
  -d '{"model":"mxbai-embed-large","prompt":"test"}'
```

#### Step 5: Decommission jarvis-cortex Ollama

```bash
# Only after all consumers verified working
sudo kubectl -n jarvis-cortex scale deploy/ollama --replicas=0

# Monitor for 24h, then delete
sudo kubectl -n jarvis-cortex delete deploy/ollama
sudo kubectl -n jarvis-cortex delete svc/ollama
sudo kubectl -n jarvis-cortex delete pvc/ollama-models  # Free 50Gi
```

### Performance Note: GPU → CPU Regression

The current ai-memory-system Ollama uses GPU for embeddings. Synapse Ollama is CPU-only. Expected impact:

| Metric                       | GPU Ollama | CPU Ollama | Acceptable?                |
| ---------------------------- | ---------- | ---------- | -------------------------- |
| Embedding latency (single)   | ~10ms      | ~50-80ms   | Yes (still < 100ms target) |
| Embedding throughput (batch) | ~500/s     | ~50/s      | Depends on workload        |
| RAM for mxbai-embed-large    | ~1GB VRAM  | ~1.3GB RAM | Yes                        |

If embedding throughput becomes a bottleneck, we can either:

1. Add GPU access to Synapse Ollama (but this contends with vLLM)
2. Run embeddings through vLLM instead (it supports embedding models)
3. Deploy a dedicated embedding service (e.g., TEI from HuggingFace)

### Phase 3 Migration: Switch to LiteLLM Gateway

After LiteLLM is deployed (Phase 3), update consumers again:

```yaml
# Final state:
OLLAMA_BASE_URL: "http://litellm-proxy.llm-infra.svc.cluster.local:8000"
OLLAMA_URL: "http://litellm-proxy.llm-infra.svc.cluster.local:8000"
```

This gives consumers automatic fallback, routing, and the ability to swap backends without any consumer changes.

---

## Risk Register

| #   | Risk                                                   | Likelihood | Impact   | Mitigation                                                         |
| --- | ------------------------------------------------------ | ---------- | -------- | ------------------------------------------------------------------ |
| R1  | RTX 5090 (Blackwell) incompatible with vLLM            | Medium     | High     | Use `--enforce-eager`, pin vLLM version, have SGLang as backup     |
| R2  | GPU contention between vLLM and jarvis-cortex Ollama   | High       | High     | Migrate ai-memory-system BEFORE deploying vLLM (Phase 1 migration) |
| R3  | LiteLLM memory leak causes OOM                         | High       | Medium   | 2 replicas + 24h rolling restart CronJob + alert at 3.5GB          |
| R4  | Ollama OOMKilled under load                            | Medium     | Medium   | Reduced limits to 64Gi, MAX_LOADED_MODELS=1, restart policy always |
| R5  | CPU embedding latency too high after GPU→CPU migration | Low        | Medium   | Benchmark first; fallback: TEI or vLLM embedding endpoint          |
| R6  | vLLM first model download takes 30+ min                | High       | Low      | Use PVC for caching, increase startupProbe timeout to 30 min       |
| R7  | Single-node failure takes everything down              | High       | Critical | Out of scope (single node cluster). Accept risk.                   |
| R8  | NetworkPolicy blocks legitimate traffic                | Medium     | High     | Deploy NetworkPolicies LAST, test each consumer before enabling    |
| R9  | Speaches STT slow on CPU for large audio               | Medium     | Low      | Acceptable for initial deploy; move to GPU if needed               |
| R10 | LiteLLM routing overhead unacceptable                  | Low        | Medium   | Benchmark; overhead should be < 5ms for proxying                   |

### Risk Dependency Chain

```
R2 (GPU contention) blocks Phase 2 deployment
  → Must complete Phase 1 migration BEFORE Phase 2
  → Migration requires: models in Synapse Ollama + consumer configmap update + verify embeddings

R1 (Blackwell compat) blocks Phase 2 validation
  → Must test nvidia-smi + vLLM startup on RTX 5090
  → If fails: evaluate SGLang as alternative (DA-4 from CLAUDE.md)
```

---

## File Inventory Summary

### Files to Create (12 total)

| Phase | File                                          | Priority |
| ----- | --------------------------------------------- | -------- |
| 2     | `manifests/apps/vllm.yaml`                    | High     |
| 2     | `manifests/infra/vllm-pvc.yaml`               | High     |
| 2     | `scripts/benchmark.sh`                        | Medium   |
| 3     | `manifests/apps/litellm.yaml`                 | High     |
| 3     | `manifests/apps/litellm-configmap.yaml`       | High     |
| 3     | `manifests/apps/litellm-rbac.yaml`            | High     |
| 3     | `manifests/apps/litellm-restart-cronjob.yaml` | High     |
| 4     | `manifests/apps/speaches.yaml`                | Medium   |
| 4     | `manifests/apps/piper-tts.yaml`               | Medium   |
| 5     | `manifests/infra/networkpolicies.yaml`        | Medium   |
| 5     | `manifests/infra/ingress.yaml`                | Medium   |
| 5     | `monitoring/alerts/synapse-alerts.yaml`       | Low      |

### Files to Modify (3 total)

| Phase | File                         | Change                             |
| ----- | ---------------------------- | ---------------------------------- |
| 1     | `manifests/apps/ollama.yaml` | Reduce limits, MAX_LOADED_MODELS=1 |
| 3+    | `Makefile`                   | Add new deploy targets per phase   |
| 4     | `scripts/health-check.sh`    | Add Speaches + Piper checks        |

### Files in Other Projects to Modify

| Phase | Project          | File                        | Change                                |
| ----- | ---------------- | --------------------------- | ------------------------------------- |
| 1     | ai-memory-system | `deploy/k8s/configmap.yaml` | Update OLLAMA_URL to cross-namespace  |
| 1     | ai-memory-system | `deploy/k8s/ollama/`        | Decommission after migration verified |

---

## Execution Order Summary

```
Phase 1: Deploy Ollama (CPU)
  ├── Apply namespace + PVC
  ├── Deploy Ollama (modified limits)
  ├── Pull models
  ├── Migrate ai-memory-system to cross-namespace Ollama
  ├── Verify embeddings work
  └── Decommission jarvis-cortex Ollama (frees GPU!)

Phase 2: Deploy vLLM (GPU)
  ├── Pre-flight: verify GPU visible to K3s
  ├── Create HF secret + PVC
  ├── Deploy vLLM
  ├── Benchmark throughput
  └── Verify RTX 5090 stability

Phase 3: Deploy LiteLLM Gateway
  ├── Create ConfigMap from config file
  ├── Deploy RBAC + LiteLLM (2 replicas)
  ├── Deploy restart CronJob
  ├── Test routing (embed → Ollama, LLM → vLLM)
  ├── Test fallback (kill vLLM → Ollama takes over)
  └── Migrate consumers to LiteLLM endpoint

Phase 4: Deploy TTS/STT
  ├── Deploy Speaches (CPU mode)
  ├── Deploy Piper TTS
  ├── Update LiteLLM config + restart
  └── Test audio endpoints

Phase 5: Security + Monitoring
  ├── Deploy Prometheus alerts
  ├── Deploy Grafana dashboard
  ├── Generate TLS cert (certbot + Route53)
  ├── Deploy Ingress
  └── Deploy NetworkPolicies (LAST — after all consumers migrated)
```
