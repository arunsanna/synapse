.PHONY: help deploy deploy-infra deploy-embed deploy-gateway deploy-phase1 \
	configure-gateway test-health test-embed logs validate clean status \
	deploy-ollama deploy-litellm

NAMESPACE ?= llm-infra
KUBECTL ?= kubectl

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# === Validation ===

validate: ## Validate all manifests (dry-run)
	$(KUBECTL) apply --dry-run=client -f manifests/infra/
	$(KUBECTL) apply --dry-run=client -f manifests/apps/
	@echo "All manifests valid."

# === Infrastructure ===

deploy-infra: ## Deploy namespace and PVCs
	$(KUBECTL) apply -f manifests/infra/namespace.yaml
	$(KUBECTL) apply -f manifests/infra/synapse-models-pvc.yaml

# === Phase 1: Embeddings + Gateway ===

deploy-embed: ## Deploy llama-server embeddings (CPU)
	$(KUBECTL) apply -f manifests/apps/llama-embed.yaml

deploy-gateway: ## Deploy Bifrost gateway
	$(KUBECTL) apply -f manifests/apps/bifrost.yaml

configure-gateway: ## Configure Bifrost to route to backends
	./scripts/configure-gateway.sh

deploy-phase1: deploy-infra deploy-embed deploy-gateway ## Deploy Phase 1 (embeddings + gateway)
	@echo ""
	@echo "Phase 1 deployed. Run 'make configure-gateway' after pods are ready."

# === Legacy (Ollama/LiteLLM — kept for reference) ===

deploy-ollama: ## Deploy Ollama (legacy)
	$(KUBECTL) apply -f manifests/apps/ollama.yaml

deploy-litellm: ## Deploy LiteLLM gateway (legacy)
	$(KUBECTL) apply -f manifests/apps/litellm.yaml

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

test-embed: ## Test embedding endpoint
	./scripts/test-embeddings.sh

# === Debugging ===

logs: ## Tail logs from all services
	$(KUBECTL) -n $(NAMESPACE) logs -f -l app.kubernetes.io/part-of=synapse --all-containers --max-log-requests=10

logs-embed: ## Tail llama-embed logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/llama-embed

logs-gateway: ## Tail Bifrost gateway logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/bifrost-gateway

logs-ollama: ## Tail Ollama logs (legacy)
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/ollama

logs-litellm: ## Tail LiteLLM logs (legacy)
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/litellm-proxy

status: ## Show all pods in namespace
	$(KUBECTL) -n $(NAMESPACE) get pods -o wide

# === Cleanup ===

clean: ## Remove all Synapse resources (destructive!)
	@echo "This will delete the entire $(NAMESPACE) namespace and all its resources."
	@echo "Press Ctrl+C to cancel, or wait 5 seconds..."
	@sleep 5
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
