#!/usr/bin/env bash
# Test embedding endpoint via Synapse gateway
set -euo pipefail

BASE_URL="${SYNAPSE_URL:-https://synapse.arunlabs.com}"

echo "=== Synapse Embedding Test ==="
echo "URL: $BASE_URL"
echo ""

# Test via gateway
echo "--- Gateway embedding test ---"
RESULT=$(curl -sf -X POST "$BASE_URL/v1/embeddings" \
  -H "Content-Type: application/json" \
  -d '{"model": "snowflake-arctic-embed2:latest", "input": "Hello world"}' 2>/dev/null) || true

if echo "$RESULT" | grep -q '"embedding"'; then
  DIMS=$(echo "$RESULT" | python3 -c "import sys,json; d=json.loads(sys.stdin.readline()); print(len(d['data'][0]['embedding']))" 2>/dev/null || echo "?")
  echo "OK: Embedding returned ($DIMS dimensions)"
else
  echo "FAIL: No embedding in response"
  echo "$RESULT" | head -5
fi

echo ""
echo "=== Done ==="
