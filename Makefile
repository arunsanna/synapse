.PHONY: help deploy deploy-infra deploy-ollama deploy-litellm deploy-phase1 \
	test-health show-routes logs clean

KUBECTL := ssh forge "sudo kubectl"
NAMESPACE := llm-infra

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# === Infrastructure ===

deploy-infra: ## Deploy namespace, PVCs, ConfigMaps
	$(KUBECTL) apply -f /opt/synapse/manifests/infra/

deploy-ollama: ## Deploy centralized Ollama (Phase 1)
	$(KUBECTL) apply -f /opt/synapse/manifests/apps/ollama.yaml

deploy-litellm: ## Deploy LiteLLM proxy (Phase 3)
	$(KUBECTL) apply -f /opt/synapse/manifests/apps/litellm.yaml

deploy-phase1: deploy-infra deploy-ollama ## Deploy Phase 1 (Ollama only)

deploy: deploy-infra ## Deploy all services
	$(KUBECTL) apply -f /opt/synapse/manifests/apps/

# === Testing ===

test-health: ## Health check all services
	@echo "--- Ollama ---"
	$(KUBECTL) -n $(NAMESPACE) exec deploy/ollama -- curl -sf http://localhost:11434/ || echo "FAIL"
	@echo "--- LiteLLM ---"
	$(KUBECTL) -n $(NAMESPACE) exec deploy/litellm-proxy -- curl -sf http://localhost:8000/health || echo "FAIL (or not deployed)"

show-routes: ## Show LiteLLM routing config
	$(KUBECTL) -n $(NAMESPACE) get configmap litellm-config -o yaml

# === Debugging ===

logs: ## Tail logs from all services
	$(KUBECTL) -n $(NAMESPACE) logs -f -l app.kubernetes.io/part-of=synapse --all-containers --max-log-requests=10

logs-ollama: ## Tail Ollama logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/ollama

logs-litellm: ## Tail LiteLLM logs
	$(KUBECTL) -n $(NAMESPACE) logs -f deploy/litellm-proxy

status: ## Show all pods in llm-infra namespace
	$(KUBECTL) -n $(NAMESPACE) get pods -o wide

# === Cleanup ===

clean: ## Remove all synapse resources
	$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
