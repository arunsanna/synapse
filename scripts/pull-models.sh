#!/usr/bin/env bash
# Pre-pull models into Ollama for instant availability
#
# Usage:
#   ./scripts/pull-models.sh                    # Pull default models
#   ./scripts/pull-models.sh model1 model2      # Pull specific models
#
# Environment:
#   SYNAPSE_NAMESPACE  Kubernetes namespace (default: llm-infra)
#   KUBECTL_CMD        kubectl command (default: kubectl)

set -euo pipefail

NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

OLLAMA_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=ollama -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -z "$OLLAMA_POD" ]; then
  echo "ERROR: No Ollama pod found in $NAMESPACE namespace"
  echo "Deploy Ollama first: make deploy-ollama"
  exit 1
fi

# Default models if none specified
if [ $# -eq 0 ]; then
  MODELS=(
    "mxbai-embed-large"   # Embeddings (~1GB)
    "llama3.1:8b"         # Small LLM (~5GB)
  )
else
  MODELS=("$@")
fi

echo "=== Pulling models into Ollama ==="
echo "Pod: $OLLAMA_POD"
echo ""

for model in "${MODELS[@]}"; do
  echo "--- Pulling: $model ---"
  $KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- ollama pull "$model"
  echo "$model: OK"
  echo ""
done

echo "=== All models pulled ==="
$KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- ollama list
