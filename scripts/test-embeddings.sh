#!/usr/bin/env bash
# Test embedding endpoint (direct and via Bifrost gateway)
set -euo pipefail

NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

echo "=== Synapse Embedding Test ==="
echo ""

# Find llama-embed pod
EMBED_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=llama-embed -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -z "$EMBED_POD" ]; then
  echo "FAIL: No llama-embed pod found"
  exit 1
fi

# Test direct llama-server (using a temp curl pod for clean output)
echo "--- Direct llama-server test ---"
RESULT=$($KUBECTL -n "$NAMESPACE" run embed-test-direct --image=curlimages/curl:8.12.1 --rm -it --restart=Never -- \
  curl -sf http://llama-embed:8081/v1/embeddings \
    -H "Content-Type: application/json" \
    -d '{"model": "mxbai-embed-large-v1-f16", "input": "Hello world"}' 2>/dev/null) || true

if echo "$RESULT" | grep -q '"embedding"'; then
  DIMS=$(echo "$RESULT" | python3 -c "import sys,json; d=json.loads(sys.stdin.readline()); print(len(d['data'][0]['embedding']))" 2>/dev/null || echo "?")
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
  GW_RESULT=$($KUBECTL -n "$NAMESPACE" run embed-test-gw --image=curlimages/curl:8.12.1 --rm -it --restart=Never -- \
    curl -sf http://bifrost-gateway:8080/v1/embeddings \
      -H "Content-Type: application/json" \
      -d '{"model": "llama-embed/mxbai-embed-large-v1-f16", "input": "Hello world"}' 2>/dev/null) || true

  if echo "$GW_RESULT" | grep -q '"embedding"'; then
    LATENCY=$(echo "$GW_RESULT" | python3 -c "import sys,json; d=json.loads(sys.stdin.readline()); print(d.get('extra_fields',{}).get('latency','?'))" 2>/dev/null || echo "?")
    echo "OK: Embedding via Bifrost gateway works (${LATENCY}ms)"
  else
    echo "FAIL: Gateway routing failed"
    echo "$GW_RESULT" | head -5
  fi
else
  echo "SKIP: Bifrost not deployed"
fi

echo ""
echo "=== Done ==="
