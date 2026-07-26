# DigitalOcean App Platform Deployment Specification

## Overview

This document describes the architecture and configuration for deploying the Inner Light Quest application to DigitalOcean App Platform (PaaS). It covers the runtime environment, ChromaDB storage strategy, environment variables, and known constraints.

For day-to-day operational procedures, see [docs/how-to/deploy.md](../how-to/deploy.md).

---

## Architecture

```
┌─────────────────────────────────────┐    startup    ┌───────────────────────────────┐
│       DigitalOcean App Platform     │◄──────────────│  DigitalOcean Spaces (fra1)   │
│       Region: fra (Frankfurt)       │  sync app/db/ │                               │
│                                     │               │  <bucket>/chroma-db/          │
│  ┌──────────────────────────────┐   │               │  ├── chroma.sqlite3           │
│  │  Web service (Python)        │   │               │  ├── <uuid-segment-dir>/      │
│  │  uvicorn / FastAPI + HTMX    │   │               │  └── <uuid-segment-dir>/      │
│  │                              │   │               └───────────────────────────────┘
│  │  ChromaDB (local, read-only) │   │
│  │  In-memory session store     │   │    requests   ┌───────────────────────────────┐
│  └──────────────────────────────┘   │──────────────►│  OpenAI API                   │
│                                     │               │  (chat completion + TTS)      │
└─────────────────────────────────────┘               └───────────────────────────────┘
```

Ingestion is **out-of-band**: it runs locally, and the resulting ChromaDB is uploaded to Spaces manually. The App Platform service never triggers ingestion.

---

## App Platform Configuration

### Buildpack

App Platform builds the image using the [Heroku Python buildpack](https://github.com/heroku/heroku-buildpack-python) with uv support.

| Setting         | Value              | Reason                                                                   |
| --------------- | ------------------ | ------------------------------------------------------------------------ |
| Stack           | `ubuntu-22`        | Required for Python 3.14 and uv package manager support                  |
| Buildpack       | Heroku Python v322 | Auto-detected; no explicit buildpack declaration needed                  |
| Package manager | `uv`               | Detected by presence of `pyproject.toml` + `uv.lock` + `.python-version` |
| Python version  | 3.14               | Set in `.python-version`; matches local development environment          |

For `uv` detection to work, `requirements.txt` and `Pipfile` must **not** be present in the repo root. They are not.

### Instance

| Setting              | Value              | Notes                                                           |
| -------------------- | ------------------ | --------------------------------------------------------------- |
| `region`             | `fra`              | Frankfurt — nearest App Platform region to Finland              |
| `instance_size_slug` | `apps-s-1vcpu-1gb` | 1 vCPU, 1 GB RAM — minimum viable for ChromaDB + onnxruntime    |
| `instance_count`     | 1                  | See [Session Store Limitations](#session-store-limitations)     |
| `http_port`          | 8080               | App Platform injects `PORT=8080`; Procfile uses `${PORT:-8000}` |

### Run Command

App Platform reads the `Procfile` automatically:

```
web: python scripts/download_db.py && uvicorn app.main:app --host=0.0.0.0 --port=${PORT:-8000}
```

`scripts/download_db.py` exits non-zero on failure, which prevents uvicorn from starting with a missing or corrupt database.

---

## ChromaDB Storage Strategy

### Problem

App Platform containers are ephemeral: the filesystem is reset on every redeploy, container restart, or scale event. The ChromaDB vector store cannot be committed to git because it grows to hundreds of megabytes after ingestion.

### Solution: Spaces-backed download at startup

The populated ChromaDB is stored in a DigitalOcean Spaces bucket (S3-compatible object storage, `fra1` region). At container startup, `scripts/download_db.py` downloads the full `app/db/` directory from Spaces before uvicorn starts.

### Why the full directory, not just `chroma.sqlite3`

ChromaDB persistent storage has two components:

| Component                                | Contents                                               | Required for                              |
| ---------------------------------------- | ------------------------------------------------------ | ----------------------------------------- |
| `chroma.sqlite3`                         | Collection metadata, document text, raw embeddings     | Reading documents and metadata            |
| UUID segment dirs (e.g. `413056d6-.../`) | HNSW vector index (`header.bin`, `data_level0.bin`, …) | Fast approximate nearest-neighbour search |

Without the HNSW index, ChromaDB cannot perform vector similarity search. Both components must be present and consistent (from the same ingestion run).

### Spaces layout

```
<SPACES_BUCKET>/
└── chroma-db/                         ← SPACES_DB_PREFIX
    ├── chroma.sqlite3
    ├── 413056d6-da8f-4289-b91b-.../   ← HNSW segment directories
    ├── 8b2de3ea-ded7-4633-b90b-.../
    └── d71b5b39-0c3d-4e5d-8b7b-.../
```

### Startup sequence

1. App Platform clones the repo and runs the buildpack (installs uv deps, no ingestion)
2. Procfile executes `python scripts/download_db.py`
3. Script connects to Spaces, lists all objects under `SPACES_DB_PREFIX`, and downloads each into `app/db/` preserving subdirectory structure
4. If the bucket prefix is empty, the script exits with an error — the container fails to start and the error surfaces in deploy logs
5. `uvicorn` starts; `app.main` loads ChromaDB from `app/db/`

### When to re-upload the database

Upload a fresh database to Spaces whenever:
- Source texts in `texts/` are added, removed, or edited
- The embedding model changes (requires full re-ingestion for consistency)

See [docs/how-to/deploy.md § Ingestion Database Deployment](../how-to/deploy.md#ingestion-database-deployment).

---

## Environment Variables

Secret values are **never** stored in `.do/app.yaml` or committed to git. They are set after app creation via `doctl` or the control panel.

### Required secrets

| Variable             | Description                                                                                                                    |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `OPENAI_API_KEY`     | OpenAI API key for chat completion and TTS                                                                                     |
| `SESSION_SECRET_KEY` | `itsdangerous` signing key for session cookies — generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `SPACES_KEY`         | Spaces access key ID                                                                                                           |
| `SPACES_SECRET`      | Spaces secret access key                                                                                                       |

### Non-secret configuration

| Variable           | Default in spec | Description                                          |
| ------------------ | --------------- | ---------------------------------------------------- |
| `ENVIRONMENT`      | `production`    | Enables the Secure cookie flag (required over HTTPS) |
| `SPACES_BUCKET`    | *(must be set)* | Spaces bucket name                                   |
| `SPACES_REGION`    | `fra1`          | Spaces region slug                                   |
| `SPACES_DB_PREFIX` | `chroma-db/`    | Object key prefix for ChromaDB in the bucket         |

### Optional (with application defaults)

`CHAT_GPT_MODEL`, `CHAT_GPT_TEMPERATURE`, `CHAT_GPT_MAX_TOKENS`, `CHAT_GPT_MAX_TOKENS_AUDIO`, `SESSION_COOKIE_NAME`, `SESSION_TTL_DAYS`, `SESSION_MAX_MESSAGES`, `SESSION_STORE_MAX_SESSIONS`, `CHAT_RATE_LIMIT`, `TTS_MODEL`, `TTS_VOICE`

---

## Session Store Limitations

Sessions are held in an **in-memory `SessionStore`** (`app/session.py`) with no persistence or cross-instance sharing. In production this means:

1. **Sessions are lost on restart.** Container restarts (OOM kill, redeploy, unhealthy health check) clear all active sessions. Users lose conversation history and are effectively logged out.
2. **Sessions cannot be shared across instances.** Running more than one instance means each container has an isolated store. A user routed to a different instance starts a new session.

`instance_count: 1` in the app spec avoids issue 2. Issue 1 is accepted for this deployment stage — conversation history is convenient but not critical.

**Upgrade path:** Replace `SessionStore` with a [DigitalOcean Managed Valkey](https://docs.digitalocean.com/products/databases/valkey/) cluster. `slowapi` rate limiting (currently per-instance, keyed by session id) would also benefit from Valkey backing.

---

## Security Considerations

- The `Secure` cookie attribute is enforced when `ENVIRONMENT=production`, ensuring cookies are only sent over HTTPS
- App Platform provides HTTPS termination automatically
- Secret environment variables are encrypted at rest and never appear in the app spec, logs, or git history
- Spaces access keys should be **restricted** to the specific bucket (not account-level keys) — generate bucket-scoped keys from the Spaces control panel
- The Spaces bucket should be **private** (default); the download script authenticates with the access key

---

## Future Upgrade Paths

| Concern                              | Current state                        | Recommended upgrade                           |
| ------------------------------------ | ------------------------------------ | --------------------------------------------- |
| Session persistence across restarts  | Lost on restart                      | Managed Valkey cluster                        |
| Multi-instance session sharing       | Isolated per container               | Valkey + sticky sessions or JWT               |
| Rate limiting across instances       | Per-instance                         | Valkey-backed `slowapi` storage               |
| ChromaDB download latency at startup | Full sync from Spaces on every start | DigitalOcean Network File Storage (NFS) mount |
| Ingestion automation                 | Manual, out-of-band                  | Scheduled job component in `.do/app.yaml`     |
