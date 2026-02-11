#!/usr/bin/env bash
# Health check for all Synapse services
set -euo pipefail

NAMESPACE="llm-infra"
KUBECTL="sudo kubectl"

echo "=== Synapse Health Check ==="
echo ""

# Check namespace exists
if ! $KUBECTL get namespace "$NAMESPACE" &>/dev/null; then
  echo "FAIL: Namespace $NAMESPACE does not exist"
  exit 1
fi

echo "Namespace: $NAMESPACE ✓"
echo ""

# Check pods
echo "--- Pods ---"
$KUBECTL get pods -n "$NAMESPACE" -o wide
echo ""

# Check services
echo "--- Services ---"
$KUBECTL get svc -n "$NAMESPACE"
echo ""

# Test Ollama
echo "--- Ollama Health ---"
if $KUBECTL -n "$NAMESPACE" get deploy/ollama &>/dev/null; then
  OLLAMA_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=ollama -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$OLLAMA_POD" ]; then
    $KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- curl -sf http://localhost:11434/ && echo " ✓" || echo "FAIL: Ollama not responding"
    echo "Loaded models:"
    $KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- curl -sf http://localhost:11434/api/tags | python3 -m json.tool 2>/dev/null || echo "  (none or error)"
  else
    echo "WARN: No Ollama pod running"
  fi
else
  echo "SKIP: Ollama not deployed"
fi
echo ""

# Test LiteLLM
echo "--- LiteLLM Health ---"
if $KUBECTL -n "$NAMESPACE" get deploy/litellm-proxy &>/dev/null; then
  LITELLM_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$LITELLM_POD" ]; then
    $KUBECTL -n "$NAMESPACE" exec "$LITELLM_POD" -- curl -sf http://localhost:8000/health && echo " ✓" || echo "FAIL: LiteLLM not responding"
  else
    echo "WARN: No LiteLLM pod running"
  fi
else
  echo "SKIP: LiteLLM not deployed"
fi
echo ""

echo "=== Done ==="
