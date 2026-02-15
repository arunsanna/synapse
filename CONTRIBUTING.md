# Contributing to Synapse

Thanks for your interest in contributing to Synapse! This document provides guidelines for contributing.

## Getting Started

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes
4. Test your changes (see Testing below)
5. Commit with conventional commits (`feat:`, `fix:`, `docs:`, `chore:`)
6. Push and open a Pull Request

## Development Setup

### Prerequisites

- Kubernetes cluster (K3s, Kind, Minikube, or managed)
- `kubectl` configured with cluster access
- `helm` v3+ (optional, for Helm-based deployment)

### Local Testing

```bash
# Validate manifests
make validate

# Deploy to your cluster
make deploy-phase1

# Run health checks
make test-health
```

## What to Contribute

### High Impact

- Helm chart parameterization
- Additional inference backend support (SGLang, TensorRT-LLM)
- Monitoring dashboards (Grafana JSON)
- Docker Compose alternative for local dev
- Documentation improvements

### Guidelines

- **K8s manifests**: Keep resource values configurable. Use comments to explain non-obvious settings.
- **Scripts**: Must be POSIX-compatible. Use `#!/usr/bin/env bash`. Include error handling.
- **Config**: Document every field. Provide sane defaults.
- **Docs**: Keep hardware-agnostic. Use "your GPU" not specific model names.

## Commit Convention

```
type(scope): description

Types: feat|fix|docs|style|refactor|perf|test|chore
```

Examples:

- `feat(routing): add latency-based routing strategy`
- `fix(tts): correct voice reference upload path`
- `docs(readme): add Helm installation instructions`

## Code Review

All PRs require:

- Manifest validation passes
- No hardcoded infrastructure-specific values
- Documentation updated if behavior changes
- At least one approval

## Reporting Issues

Use GitHub Issues with the provided templates. Include:

- Kubernetes version
- Backend versions (llama-embed, Chatterbox, faster-whisper, pyannote, DeepFilterNet)
- Steps to reproduce
- Expected vs actual behavior

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
