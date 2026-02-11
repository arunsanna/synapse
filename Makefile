.PHONY: help deploy deploy-infra deploy-ollama deploy-litellm deploy-phase1 \
	test-health show-routes logs validate clean

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
	$(KUBECTL) apply -f manifests/infra/

deploy-ollama: ## Deploy Ollama (CPU inference)
	$(KUBECTL) apply -f manifests/apps/ollama.yaml

deploy-litellm: ## Deploy LiteLLM gateway
	$(KUBECTL) apply -f manifests/apps/litellm.yaml

deploy-phase1: deploy-infra deploy-ollama ## Deploy Phase 1 (Ollama only)

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

show-routes: ## Show LiteLLM routing config
	$(KUBECTL) -n $(NAMESPACE) get configmap litellm-config -o yaml

# === Debugging ===

logs: ## Tail logs from all services
	$(KUBECTL) -n $(NAMESPACE) logs -f -l app.kubernetes.io/part-of=synapse --all-containers --max-log-requests=10

logs-ollama: ## Tail Ollama logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/ollama

logs-litellm: ## Tail LiteLLM logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/litellm-proxy

status: ## Show all pods in namespace
	$(KUBECTL) -n $(NAMESPACE) get pods -o wide

# === Cleanup ===

clean: ## Remove all Synapse resources (destructive!)
	@echo "This will delete the entire $(NAMESPACE) namespace and all its resources."
	@echo "Press Ctrl+C to cancel, or wait 5 seconds..."
	@sleep 5
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
