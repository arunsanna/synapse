#!/usr/bin/env bash
# Test embedding endpoint (direct and via gateway)
set -euo pipefail

NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

echo "=== Synapse Embedding Test ==="
echo ""

# Find pods
EMBED_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=llama-embed -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -z "$EMBED_POD" ]; then
  echo "FAIL: No llama-embed pod found"
  exit 1
fi

# Test direct llama-server
echo "--- Direct llama-server test ---"
RESULT=$($KUBECTL -n "$NAMESPACE" exec "$EMBED_POD" -- \
  curl -sf http://localhost:8081/v1/embeddings \
    -H "Content-Type: application/json" \
    -d '{"model": "mxbai-embed-large-v1-f16", "input": "Hello world"}' 2>&1) || true

if echo "$RESULT" | grep -q '"embedding"'; then
  DIMS=$(echo "$RESULT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['data'][0]['embedding']))" 2>/dev/null || echo "?")
  echo "OK: Embedding returned ($DIMS dimensions)"
else
  echo "FAIL: No embedding in response"
  echo "$RESULT" | head -5
fi

echo ""

# Test via Bifrost gateway (if deployed)
BIFROST_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=bifrost-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -n "$BIFROST_POD" ]; then
  echo "--- Bifrost gateway test ---"
  GW_RESULT=$($KUBECTL -n "$NAMESPACE" exec "$BIFROST_POD" -- \
    curl -sf http://llama-embed.llm-infra.svc.cluster.local:8081/v1/embeddings \
      -H "Content-Type: application/json" \
      -d '{"model": "mxbai-embed-large-v1-f16", "input": "Hello world"}' 2>&1) || true

  if echo "$GW_RESULT" | grep -q '"embedding"'; then
    echo "OK: Embedding via cross-service routing works"
  else
    echo "WARN: Gateway routing may need configuration (run: ./scripts/configure-gateway.sh)"
  fi
else
  echo "SKIP: Bifrost not deployed"
fi

echo ""
echo "=== Done ==="
