#!/usr/bin/env bash
# Health check for all Synapse services
set -euo pipefail

BASE_URL="${SYNAPSE_URL:-https://synapse.arunlabs.com}"
NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

echo "=== Synapse Health Check ==="
echo "URL: $BASE_URL"
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

# Test gateway health endpoint (aggregates all backends)
echo "--- Gateway Health ---"
HEALTH=$(curl -sf "$BASE_URL/health" 2>/dev/null) || true

if [ -n "$HEALTH" ]; then
  echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"
else
  echo "FAIL: Gateway unreachable at $BASE_URL"
fi

echo ""
echo "=== Done ==="
