.PHONY: help deploy deploy-infra deploy-embed deploy-gateway deploy-tts deploy-phase1 \
	deploy-llm deploy-stt deploy-speaker deploy-audio \
	build-gateway test-health test-embed test-tts logs logs-embed logs-llm \
	logs-gateway logs-tts logs-stt logs-speaker logs-audio validate clean \
	status show-routes

NAMESPACE ?= llm-infra
KUBECTL ?= kubectl
REGISTRY ?= registry.arunlabs.com
GATEWAY_IMAGE ?= $(REGISTRY)/synapse-gateway:latest

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# === Validation ===

validate: ## Validate all manifests (dry-run)
	$(KUBECTL) apply --dry-run=client -f manifests/infra/
	$(KUBECTL) apply --dry-run=client -f manifests/apps/
	@echo "All manifests valid."

# === Build ===

build-gateway: ## Build and push gateway image to registry
	docker build -t $(GATEWAY_IMAGE) gateway/
	docker push $(GATEWAY_IMAGE)

# === Infrastructure ===

deploy-infra: ## Deploy namespace, PVCs, and ingress
	$(KUBECTL) apply -f manifests/infra/namespace.yaml
	$(KUBECTL) apply -f manifests/infra/synapse-models-pvc.yaml
	$(KUBECTL) apply -f manifests/infra/pvc-voices.yaml
	$(KUBECTL) apply -f manifests/infra/ingress.yaml

# === Phase 1: Custom Gateway + Embeddings + TTS ===

deploy-embed: ## Deploy llama-server embeddings (CPU)
	$(KUBECTL) apply -f manifests/apps/llama-embed.yaml

deploy-llm: ## Deploy llama.cpp router backend (chat/completions + model load/unload)
	$(KUBECTL) apply -f manifests/apps/llama-router.yaml

deploy-gateway: ## Deploy Synapse custom gateway
	$(KUBECTL) apply -f manifests/apps/gateway.yaml

deploy-tts: ## Deploy Chatterbox TTS backend (GPU)
	$(KUBECTL) apply -f manifests/apps/chatterbox-tts.yaml

deploy-phase1: deploy-infra deploy-embed deploy-tts deploy-gateway ## Deploy Phase 1 (gateway + embeddings + TTS)
	@echo ""
	@echo "Phase 1 deployed. Custom gateway at synapse.arunlabs.com"
	@echo "Run 'make test-health' to verify."

deploy-stt: ## Deploy whisper-stt (faster-whisper, CPU)
	$(KUBECTL) apply -f manifests/apps/whisper-stt.yaml

deploy-speaker: ## Deploy pyannote-speaker (diarization, CPU)
	$(KUBECTL) apply -f manifests/apps/pyannote-speaker.yaml

deploy-audio: ## Deploy deepfilter-audio (noise reduction, CPU)
	$(KUBECTL) apply -f manifests/apps/deepfilter-audio.yaml

deploy: deploy-infra ## Deploy all services
	$(KUBECTL) apply -f manifests/apps/

# === Testing ===

test-health: ## Health check all services
	@echo "--- Namespace ---"
	@$(KUBECTL) get namespace $(NAMESPACE) 2>/dev/null && echo "OK" || echo "MISSING"
	@echo ""
	@echo "--- Pods ---"
	@$(KUBECTL) -n $(NAMESPACE) get pods -o wide 2>/dev/null || echo "No pods found"
	@echo ""
	@echo "--- Services ---"
	@$(KUBECTL) -n $(NAMESPACE) get svc 2>/dev/null || echo "No services found"
	@echo ""
	@echo "--- Gateway Health ---"
	@curl -sf https://synapse.arunlabs.com/health 2>/dev/null | python3 -m json.tool || echo "Gateway unreachable"

test-embed: ## Test embedding endpoint
	./scripts/test-embeddings.sh

test-tts: ## Test TTS synthesis
	@echo "--- TTS Synthesize ---"
	@curl -sf -X POST https://synapse.arunlabs.com/tts/synthesize \
		-H "Content-Type: application/json" \
		-d '{"text": "Hello from Synapse", "language": "en"}' \
		-o /tmp/synapse_test.wav && echo "OK: /tmp/synapse_test.wav" || echo "FAILED"

show-routes: ## Show all registered routes
	@echo "Gateway routes:"
	@echo "  POST /v1/embeddings              -> llama-embed"
	@echo "  POST /v1/chat/completions        -> llama-router"
	@echo "  GET  /v1/models                  -> all LLM backends"
	@echo "  GET  /models                     -> llama-router model status"
	@echo "  POST /models/load                -> llama-router load model"
	@echo "  POST /models/unload              -> llama-router unload model"
	@echo "  GET  /voices                     -> gateway (local)"
	@echo "  POST /voices                     -> gateway (local)"
	@echo "  POST /voices/{id}/references     -> gateway (local)"
	@echo "  DELETE /voices/{id}              -> gateway (local)"
	@echo "  POST /tts/synthesize             -> chatterbox-tts"
	@echo "  POST /tts/stream                 -> chatterbox-tts"
	@echo "  POST /tts/interpolate            -> chatterbox-tts"
	@echo "  GET  /tts/languages              -> gateway (static)"
	@echo "  POST /stt/transcribe             -> whisper-stt"
	@echo "  POST /stt/detect-language        -> whisper-stt"
	@echo "  POST /stt/stream                 -> whisper-stt (SSE)"
	@echo "  POST /speakers/diarize           -> pyannote-speaker"
	@echo "  POST /speakers/verify            -> pyannote-speaker"
	@echo "  POST /audio/denoise              -> deepfilter-audio"
	@echo "  POST /audio/convert              -> deepfilter-audio"
	@echo "  GET  /health                     -> aggregated"

# === Debugging ===

logs: ## Tail logs from all services
	$(KUBECTL) -n $(NAMESPACE) logs -f -l app.kubernetes.io/part-of=synapse --all-containers --max-log-requests=10

logs-embed: ## Tail llama-embed logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/llama-embed

logs-llm: ## Tail llama-router logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/llama-router

logs-gateway: ## Tail Synapse gateway logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/synapse-gateway

logs-tts: ## Tail Chatterbox TTS logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/chatterbox-tts

logs-stt: ## Tail whisper-stt logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/whisper-stt

logs-speaker: ## Tail pyannote-speaker logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/pyannote-speaker

logs-audio: ## Tail deepfilter-audio logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/deepfilter-audio

status: ## Show all pods in namespace
	$(KUBECTL) -n $(NAMESPACE) get pods -o wide

# === Cleanup ===

clean: ## Remove all Synapse resources (destructive!)
	@echo "This will delete the entire $(NAMESPACE) namespace and all its resources."
	@echo "Press Ctrl+C to cancel, or wait 5 seconds..."
	@sleep 5
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
