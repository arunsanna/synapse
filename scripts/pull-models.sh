#!/usr/bin/env bash
# Pre-pull models into Ollama for instant availability
set -euo pipefail

NAMESPACE="llm-infra"
KUBECTL="sudo kubectl"

OLLAMA_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=ollama -o jsonpath='{.items[0].metadata.name}')

if [ -z "$OLLAMA_POD" ]; then
  echo "ERROR: No Ollama pod found in $NAMESPACE"
  exit 1
fi

echo "=== Pulling models into Ollama ==="

# Phase 1 models
MODELS=(
  "mxbai-embed-large"   # Embeddings (~1GB)
  "llama3.1:8b"         # Small LLM fallback (~5GB)
)

for model in "${MODELS[@]}"; do
  echo ""
  echo "--- Pulling: $model ---"
  $KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- ollama pull "$model"
  echo "✓ $model pulled"
done

echo ""
echo "=== All models pulled ==="
$KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- ollama list
