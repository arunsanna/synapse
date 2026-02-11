#!/usr/bin/env bash
# Configure Bifrost gateway to route to Synapse backends
#
# Run after deploying Bifrost and backend services:
#   ./scripts/configure-gateway.sh
#
# This script is idempotent — safe to re-run after pod restarts.
#
# Environment:
#   SYNAPSE_NAMESPACE  Kubernetes namespace (default: llm-infra)
#   KUBECTL_CMD        kubectl command (default: kubectl)

set -euo pipefail

NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

BIFROST_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=bifrost-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -z "$BIFROST_POD" ]; then
  echo "ERROR: No Bifrost pod found in $NAMESPACE namespace"
  echo "Deploy Bifrost first: make deploy-gateway"
  exit 1
fi

echo "=== Configuring Bifrost Gateway ==="
echo "Pod: $BIFROST_POD"
echo ""

# Configure the embeddings backend (llama-server)
# Bifrost uses "openai" provider type for any OpenAI-compatible backend
echo "--- Configuring embeddings backend (llama-embed) ---"
$KUBECTL -n "$NAMESPACE" exec "$BIFROST_POD" -- \
  curl -sf --max-time 10 \
    -X POST http://localhost:8080/api/v1/providers \
    -H "Content-Type: application/json" \
    -d '{
      "provider": "openai",
      "keys": [
        {
          "name": "llama-embed",
          "value": "no-key-needed",
          "models": [],
          "weight": 1.0
        }
      ],
      "network_config": {
        "base_url": "http://llama-embed.llm-infra.svc.cluster.local:8081"
      }
    }'
echo ""
echo "Embeddings backend configured: llama-embed:8081"

# === Future backends (uncomment as you deploy them) ===

# Phase 2: LLM (Qwen3-32B via llama-server GPU)
# echo "--- Configuring LLM backend (llama-llm) ---"
# $KUBECTL -n "$NAMESPACE" exec "$BIFROST_POD" -- \
#   curl -sf -X POST http://localhost:8080/api/v1/providers \
#   -H "Content-Type: application/json" \
#   -d '{
#     "provider": "openai",
#     "keys": [{"name": "llama-llm", "value": "no-key-needed", "models": [], "weight": 1.0}],
#     "network_config": {
#       "base_url": "http://llama-llm.llm-infra.svc.cluster.local:8082"
#     }
#   }'

# Phase 3: STT (Speaches / faster-whisper)
# Phase 3: TTS (Coqui XTTS-v2)
# Phase 4: Voice Cloning (OpenVoice v2)

echo ""
echo "=== Gateway configured ==="
echo ""
echo "Endpoints:"
echo "  Gateway:    http://bifrost-gateway.llm-infra.svc.cluster.local:8080"
echo "  Embeddings: http://llama-embed.llm-infra.svc.cluster.local:8081 (direct)"
echo ""
echo "Test embedding via gateway:"
echo '  curl http://bifrost-gateway:8080/openai/v1/embeddings \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"model": "mxbai-embed-large-v1-f16", "input": "test embedding"}'"'"''
