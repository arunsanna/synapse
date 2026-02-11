#!/usr/bin/env bash
# Bifrost gateway provider configuration reference
#
# Bifrost reads config from a ConfigMap mounted at /app/data/config.json.
# To add/change providers, edit manifests/apps/bifrost.yaml (ConfigMap section)
# and re-apply: kubectl apply -f manifests/apps/bifrost.yaml
#
# This script validates the current configuration is loaded correctly.
#
# Environment:
#   SYNAPSE_NAMESPACE  Kubernetes namespace (default: llm-infra)
#   KUBECTL_CMD        kubectl command (default: kubectl)

set -euo pipefail

NAMESPACE="${SYNAPSE_NAMESPACE:-llm-infra}"
KUBECTL="${KUBECTL_CMD:-kubectl}"

echo "=== Bifrost Gateway Configuration ==="
echo ""

# Verify ConfigMap exists
if ! $KUBECTL -n "$NAMESPACE" get configmap bifrost-config &>/dev/null; then
  echo "ERROR: bifrost-config ConfigMap not found"
  echo "Deploy Bifrost first: make deploy-gateway"
  exit 1
fi

echo "--- ConfigMap ---"
$KUBECTL -n "$NAMESPACE" get configmap bifrost-config -o jsonpath='{.data.config\.json}' | python3 -m json.tool 2>/dev/null || \
  $KUBECTL -n "$NAMESPACE" get configmap bifrost-config -o jsonpath='{.data.config\.json}'
echo ""

# Verify Bifrost pod is running
BIFROST_POD=$($KUBECTL -n "$NAMESPACE" get pod -l app=bifrost-gateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$BIFROST_POD" ]; then
  echo "WARN: No Bifrost pod running"
  exit 1
fi

echo "--- Pod: $BIFROST_POD ---"
echo "Status: $($KUBECTL -n "$NAMESPACE" get pod "$BIFROST_POD" -o jsonpath='{.status.phase}')"
echo ""

# Test gateway health
echo "--- Health Check ---"
$KUBECTL -n "$NAMESPACE" run curl-validate --image=curlimages/curl:8.12.1 --rm -it --restart=Never -- \
  curl -sf http://bifrost-gateway:8080/health 2>/dev/null || echo "FAIL: Health check failed"
echo ""

echo ""
echo "=== Current Providers ==="
echo "  llama-embed → http://llama-embed:8081 (mxbai-embed-large-v1, CPU)"
echo ""
echo "=== Endpoints ==="
echo "  Gateway:    http://bifrost-gateway.llm-infra.svc.cluster.local:8080"
echo "  Embeddings: http://llama-embed.llm-infra.svc.cluster.local:8081 (direct)"
echo ""
echo "Test embedding via gateway:"
echo '  curl http://bifrost-gateway:8080/v1/embeddings \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"model": "llama-embed/mxbai-embed-large-v1-f16", "input": "test"}'"'"''
echo ""
echo "To add providers: edit manifests/apps/bifrost.yaml (ConfigMap) and re-apply."
