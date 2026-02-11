#!/usr/bin/env bash
# Health check for all Synapse services
set -euo pipefail

NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

echo "=== Synapse Health Check ==="
echo "Namespace: $NAMESPACE"
echo ""

# Check namespace exists
if ! $KUBECTL get namespace "$NAMESPACE" &>/dev/null; then
  echo "FAIL: Namespace $NAMESPACE does not exist"
  exit 1
fi
echo "Namespace: OK"
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
    if $KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- curl -sf http://localhost:11434/ >/dev/null 2>&1; then
      echo "Ollama: OK"
    else
      echo "Ollama: FAIL (not responding)"
    fi
    echo "Loaded models:"
    $KUBECTL -n "$NAMESPACE" exec "$OLLAMA_POD" -- curl -sf http://localhost:11434/api/tags 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  (none or error)"
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
    if $KUBECTL -n "$NAMESPACE" exec "$LITELLM_POD" -- curl -sf http://localhost:8000/health >/dev/null 2>&1; then
      echo "LiteLLM: OK"
    else
      echo "LiteLLM: FAIL (not responding)"
    fi
  else
    echo "WARN: No LiteLLM pod running"
  fi
else
  echo "SKIP: LiteLLM not deployed"
fi

echo ""
echo "=== Done ==="
