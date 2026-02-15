# Synapse Deployment Guide

Step-by-step guide for deploying Synapse on a Kubernetes cluster.

---

## Prerequisites

| Requirement          | Minimum             | Notes                                     |
| -------------------- | ------------------- | ----------------------------------------- |
| Kubernetes           | v1.26+              | K3s, Kind, Minikube, EKS, GKE all work    |
| kubectl              | Configured          | Must have cluster access                  |
| Storage              | 200Gi+              | For model storage (local-path, EBS, etc.) |
| RAM                  | 64Gi+               | More RAM = more concurrent models         |
| CPU                  | 8+ cores            | More cores = faster CPU inference         |
| GPU (optional)       | NVIDIA with drivers | Only needed for Phase 2+ (vLLM)           |
| NVIDIA Device Plugin | v0.14+              | Only if using GPU                         |

## Phase 1: Centralized Ollama (CPU Only)

This phase deploys Ollama as a centralized CPU inference backend. No GPU required.

### 1.1 Deploy Infrastructure

```bash
# Create namespace and persistent storage
kubectl apply -f manifests/infra/
```

Verify:

```bash
kubectl get namespace llm-infra
kubectl get pvc -n llm-infra
```

### 1.2 Deploy Ollama

```bash
kubectl apply -f manifests/apps/ollama.yaml
```

Wait for the pod to be ready:

```bash
kubectl -n llm-infra wait --for=condition=ready pod -l app=ollama --timeout=120s
```

### 1.3 Tune Resources

Edit `manifests/apps/ollama.yaml` to match your node:

```yaml
resources:
  requests:
    memory: 32Gi # Minimum: enough for 1 model
    cpu: 8 # Minimum: 8 cores
  limits:
    memory: 96Gi # Adjust: ~40GB per concurrent model + overhead
    cpu: 32 # Adjust: your node's CPU count
```

**RAM sizing guide:**

| Available RAM | `OLLAMA_MAX_LOADED_MODELS` | `memory.limits` |
| ------------- | -------------------------- | --------------- |
| 32Gi          | 1                          | 24Gi            |
| 64Gi          | 1                          | 48Gi            |
| 128Gi         | 2                          | 96Gi            |
| 256Gi+        | 3-4                        | 192Gi           |

### 1.4 Pull Models

```bash
# Pull default models (embeddings + small LLM)
./scripts/pull-models.sh

# Or pull specific models
./scripts/pull-models.sh mistral:7b codellama:7b
```

### 1.5 Verify

```bash
./scripts/health-check.sh
```

### 1.6 Point Your Applications

Update your applications to use the centralized Ollama:

```
http://ollama.llm-infra.svc.cluster.local:11434
```

This is an OpenAI-compatible API. Example with curl:

```bash
curl http://ollama.llm-infra.svc.cluster.local:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Phase 1 Success Criteria

- [ ] Ollama pod running and healthy
- [ ] Models loaded and responding
- [ ] Applications migrated to centralized endpoint
- [ ] Embedding requests completing < 100ms p95
- [ ] LLM requests completing < 5s TTFT

---

## Phase 2: Add vLLM (GPU Inference)

This phase adds GPU-accelerated inference for large models.

### 2.1 Prerequisites

- NVIDIA GPU with CUDA drivers
- [NVIDIA Device Plugin](https://github.com/NVIDIA/k8s-device-plugin) installed
- GPU visible: `kubectl get nodes -o json | jq '.items[].status.capacity["nvidia.com/gpu"]'`

### 2.2 Deploy vLLM

Create `manifests/apps/vllm.yaml` for your setup:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm-inference
  namespace: llm-infra
  labels:
    app: vllm-inference
    app.kubernetes.io/part-of: synapse
spec:
  replicas: 1
  selector:
    matchLabels:
      app: vllm-inference
  template:
    metadata:
      labels:
        app: vllm-inference
        app.kubernetes.io/part-of: synapse
    spec:
      containers:
        - name: vllm
          image: vllm/vllm-openai:latest
          args:
            - --model=meta-llama/Meta-Llama-3.1-8B-Instruct
            - --gpu-memory-utilization=0.85
            - --max-model-len=8192
          resources:
            limits:
              nvidia.com/gpu: 1
              memory: 64Gi
            requests:
              nvidia.com/gpu: 1
              memory: 32Gi
          ports:
            - containerPort: 8000
              name: http
          # vLLM takes a while to load models on first start
          startupProbe:
            httpGet:
              path: /health
              port: 8000
            failureThreshold: 60
            periodSeconds: 30
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 10
---
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
      name: http
  type: ClusterIP
```

> **Note:** Do NOT name the service `vllm` — this creates environment variable conflicts with vLLM internals. Use `vllm-inference` or similar.

```bash
kubectl apply -f manifests/apps/vllm.yaml
```

### 2.3 Verify GPU Inference

```bash
curl http://vllm-inference.llm-infra.svc.cluster.local:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Phase 2 Success Criteria

- [ ] vLLM pod running with GPU access
- [ ] Model loaded and responding
- [ ] Tokens/second > 100 for 8B model
- [ ] GPU VRAM usage within budget

---

## Phase 3: Add LiteLLM Gateway

This phase deploys the unified routing layer.

### 3.1 Create ConfigMap

```bash
kubectl create configmap litellm-config \
  --from-file=config.yaml=config/litellm-config.yaml \
  -n llm-infra
```

### 3.2 Deploy LiteLLM

Create `manifests/apps/litellm.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: litellm-proxy
  namespace: llm-infra
  labels:
    app: litellm-proxy
    app.kubernetes.io/part-of: synapse
spec:
  replicas: 2
  selector:
    matchLabels:
      app: litellm-proxy
  template:
    metadata:
      labels:
        app: litellm-proxy
        app.kubernetes.io/part-of: synapse
    spec:
      containers:
        - name: litellm
          image: ghcr.io/berriai/litellm:latest
          command: ["litellm", "--config", "/config/config.yaml"]
          volumeMounts:
            - name: config
              mountPath: /config
          ports:
            - containerPort: 8000
          resources:
            requests:
              memory: 2Gi
              cpu: 2
            limits:
              memory: 4Gi
              cpu: 4
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 5
      volumes:
        - name: config
          configMap:
            name: litellm-config
---
apiVersion: v1
kind: Service
metadata:
  name: litellm-proxy
  namespace: llm-infra
spec:
  selector:
    app: litellm-proxy
  ports:
    - port: 8000
      targetPort: 8000
  type: ClusterIP
```

```bash
kubectl apply -f manifests/apps/litellm.yaml
```

### 3.3 (Recommended) Add Rolling Restart

LiteLLM has known memory leak behavior under sustained load. Add a daily restart CronJob:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: litellm-restart
  namespace: llm-infra
spec:
  schedule: "0 4 * * *" # Daily at 4 AM
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: litellm-restart-sa
          containers:
            - name: restart
              image: bitnami/kubectl:latest
              command:
                - kubectl
                - rollout
                - restart
                - deployment/litellm-proxy
                - -n
                - llm-infra
          restartPolicy: OnFailure
```

### 3.4 Update Applications

Point all applications to the unified gateway:

```
http://litellm-proxy.llm-infra.svc.cluster.local:8000
```

This single endpoint handles all model types — embeddings, LLMs, TTS, STT.

### Phase 3 Success Criteria

- [ ] LiteLLM healthy with 2 replicas
- [ ] Routing works: embedding requests go to Ollama, LLM requests go to vLLM
- [ ] Fallback works: simulate vLLM failure, verify Ollama takes over
- [ ] Routing overhead < 20ms p95
- [ ] All applications migrated to gateway endpoint

---

## Phase 4: Add TTS/STT Services

Deploy speech services behind the gateway. Use any OpenAI-compatible TTS/STT server.

### Example: Speaches (faster-whisper for STT)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: speaches-stt
  namespace: llm-infra
spec:
  replicas: 1
  selector:
    matchLabels:
      app: speaches-stt
  template:
    metadata:
      labels:
        app: speaches-stt
        app.kubernetes.io/part-of: synapse
    spec:
      containers:
        - name: speaches
          image: ghcr.io/speaches-ai/speaches:latest
          env:
            - name: WHISPER_MODEL
              value: "large-v3"
          resources:
            limits:
              memory: 16Gi
          ports:
            - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: speaches-stt
  namespace: llm-infra
spec:
  selector:
    app: speaches-stt
  ports:
    - port: 8002
      targetPort: 8000
  type: ClusterIP
```

### Example: Piper TTS (CPU)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: piper-tts
  namespace: llm-infra
spec:
  replicas: 1
  selector:
    matchLabels:
      app: piper-tts
  template:
    metadata:
      labels:
        app: piper-tts
        app.kubernetes.io/part-of: synapse
    spec:
      containers:
        - name: openedai-speech
          image: ghcr.io/matatonic/openedai-speech:latest
          env:
            - name: TTS_BACKEND
              value: "piper"
          resources:
            limits:
              memory: 4Gi
              cpu: "4"
          ports:
            - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: piper-tts
  namespace: llm-infra
spec:
  selector:
    app: piper-tts
  ports:
    - port: 8003
      targetPort: 8000
  type: ClusterIP
```

Then add routes to your LiteLLM config and reload.

---

## Phase 5: Monitoring

### Deploy DCGM Exporter (GPU Metrics)

```bash
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts
helm install dcgm-exporter gpu-helm-charts/dcgm-exporter -n llm-infra
```

### Key Metrics to Monitor

| Metric                | Source             | Alert Threshold   |
| --------------------- | ------------------ | ----------------- |
| GPU VRAM usage        | DCGM Exporter      | > 90% for 5 min   |
| GPU temperature       | DCGM Exporter      | > 85C for 5 min   |
| vLLM queue depth      | vLLM `/metrics`    | > 10 waiting      |
| LiteLLM fallback rate | LiteLLM `/metrics` | > 0.1/s for 5 min |
| Ollama health         | Custom             | Down for 1 min    |

See `monitoring/alerts/` for pre-built Prometheus alert rules.

---

## Rollback

At any phase, you can rollback:

```bash
# Remove a specific service
kubectl delete -f manifests/apps/litellm.yaml

# Remove everything
make clean
```

Applications should have fallback behavior (direct Ollama access) for resilience during rollbacks.

---

## Troubleshooting

### Ollama pod OOMKilled

Reduce `OLLAMA_MAX_LOADED_MODELS` or increase `memory.limits`:

```bash
kubectl -n llm-infra edit deploy/ollama
```

### vLLM CUDA out of memory

Lower `--gpu-memory-utilization` (default 0.9, try 0.75):

```bash
kubectl -n llm-infra edit deploy/vllm-inference
```

### LiteLLM routing to wrong backend

Check the config:

```bash
make show-routes
```

Verify backend health:

```bash
./scripts/health-check.sh
```

### Models slow to load

First pull takes time (downloading). Subsequent starts use the PVC cache. Increase startup probe timeout if needed.
