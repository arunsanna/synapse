# Repository Organization

This document defines the working structure for Synapse after cleanup.

## Active Layout

```text
synapse/
├── gateway/                  # FastAPI gateway source
├── manifests/                # Kubernetes manifests (apps + infra)
├── config/                   # Local backend registry source
├── scripts/                  # Operational test and health scripts
├── docs/                     # Active docs only
│   ├── API.md
│   ├── INTEGRATION-GUIDE.md
│   ├── ARCHITECTURE.md
│   └── diagrams/
├── archive/                  # Superseded assets and docs
└── docs/archive/             # Legacy roadmap/build/deployment timeline docs
```

## Rules

- Keep only operationally relevant docs in `docs/`.
- Store diagram binaries in `docs/diagrams/`.
- Archive superseded docs/assets under `archive/`.
- Keep historical planning content in `docs/archive/` unless replaced by stronger source-of-truth docs.

## Documentation Update Checklist

Use this checklist for each functional change:

1. Update `README.md` for user-visible behavior changes.
2. Update `docs/API.md` for request/response/schema/path changes.
3. Update `docs/INTEGRATION-GUIDE.md` for workflow changes.
4. Update `docs/ARCHITECTURE.md` for topology or routing changes.
5. Add a dated note to `CHANGELOG.md`.
6. Move superseded docs/assets into `archive/`.

## Archive Criteria

Move content to `archive/` when all of the following are true:

- It is not required to deploy, operate, or integrate with current Synapse.
- It has been superseded by newer docs/code.
- Keeping it in active folders creates ambiguity or clutter.
