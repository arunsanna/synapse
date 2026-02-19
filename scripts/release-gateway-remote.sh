#!/usr/bin/env bash
set -euo pipefail

FORGE_HOST="${FORGE_HOST:-forge}"
NAMESPACE="${NAMESPACE:-llm-infra}"
IMAGE_REPO="${IMAGE_REPO:-registry.arunlabs.com/synapse-gateway}"
TAG="${TAG:-$(date -u +%Y%m%d%H%M%S)}"
REMOTE_DIR="${REMOTE_DIR:-}"

if [[ -z "$REMOTE_DIR" ]]; then
  REMOTE_HOME="$(ssh "$FORGE_HOST" 'printf "%s" "$HOME"')"
  REMOTE_DIR="${REMOTE_HOME}/synapse-build"
fi

if [[ ! -d "gateway" || ! -f "manifests/apps/gateway.yaml" ]]; then
  echo "Run this script from the synapse repo root." >&2
  exit 1
fi

echo "[1/4] Syncing repo to ${FORGE_HOST}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.DS_Store' \
  ./ "${FORGE_HOST}:${REMOTE_DIR}/"

echo "[2/4] Building and pushing ${IMAGE_REPO}:${TAG} on ${FORGE_HOST}"
DIGEST="$(
  ssh "$FORGE_HOST" bash -s -- "$REMOTE_DIR" "$IMAGE_REPO" "$TAG" <<'EOF'
set -euo pipefail
REMOTE_DIR="$1"
IMAGE_REPO="$2"
TAG="$3"
IMAGE="${IMAGE_REPO}:${TAG}"
cd "$REMOTE_DIR"
docker build -t "$IMAGE" gateway >&2
docker push "$IMAGE" >&2
docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}'
EOF
)"

if [[ "$DIGEST" != "${IMAGE_REPO}@sha256:"* ]]; then
  echo "Unexpected digest from remote build: $DIGEST" >&2
  exit 1
fi

echo "[3/4] Deploying gateway image digest to k3s: ${DIGEST}"
ssh "$FORGE_HOST" bash -s -- "$REMOTE_DIR" "$NAMESPACE" "$DIGEST" <<'EOF'
set -euo pipefail
REMOTE_DIR="$1"
NAMESPACE="$2"
DIGEST="$3"
sudo kubectl -n "$NAMESPACE" apply -f "$REMOTE_DIR/manifests/apps/gateway.yaml"
sudo kubectl -n "$NAMESPACE" patch deploy synapse-gateway --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"Always"}]'
sudo kubectl -n "$NAMESPACE" set image deployment/synapse-gateway gateway="$DIGEST"
sudo kubectl -n "$NAMESPACE" rollout status deployment/synapse-gateway --timeout=300s
EOF

echo "[4/4] Verifying running pod image"
ssh "$FORGE_HOST" "sudo kubectl -n '$NAMESPACE' get pods -l app=synapse-gateway -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image,IMAGEID:.status.containerStatuses[0].imageID --no-headers"

echo "Remote release complete: ${DIGEST}"
