# How to Deploy

Operational procedures for deploying and maintaining the application on DigitalOcean App Platform.

For the deployment architecture and design rationale, see [docs/specifications/digitalocean_deployment.md](../specifications/digitalocean_deployment.md).

---

## Prerequisites

- [`doctl`](https://docs.digitalocean.com/reference/doctl/how-to/install/) installed and authenticated (`doctl auth init`)
- [`awscli`](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed (used for syncing files to Spaces)
- A DigitalOcean Spaces bucket in `fra1` — [create one in the control panel](https://cloud.digitalocean.com/spaces)
- Spaces access keys — generate from **API → Spaces Keys** in the control panel
- `uv` installed locally (for running ingestion)

---

## First-Time App Deploy

### 1. Validate the app spec

```bash
doctl apps create --spec .do/app.yaml --no-wait
```

If the spec is invalid, `doctl` will print errors without creating the app. Fix any issues before proceeding.

### 2. Create the app

```bash
doctl apps create --spec .do/app.yaml
```

Note the `id` from the output — this is your `<app-id>`.

### 3. Set secrets

Secret values are not stored in `.do/app.yaml`. Set them through the [App Platform control panel](https://cloud.digitalocean.com/apps) under **Settings → Environment Variables**, or via `doctl`:

```bash
APP_ID=<your-app-id>

doctl apps update $APP_ID --spec - << 'EOF'
name: inner-light-quest
region: fra
services:
  - name: web
    envs:
      - key: OPENAI_API_KEY
        value: "<your-openai-api-key>"
        type: SECRET
      - key: SESSION_SECRET_KEY
        value: "<generate-with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'>"
        type: SECRET
      - key: SPACES_KEY
        value: "<your-spaces-access-key-id>"
        type: SECRET
      - key: SPACES_SECRET
        value: "<your-spaces-secret-access-key>"
        type: SECRET
      - key: SPACES_BUCKET
        value: "<your-bucket-name>"
        type: GENERAL
EOF
```

### 4. Upload the ingestion database

The app will fail to start until the Spaces bucket contains a populated ChromaDB. Follow the [Ingestion Database Deployment](#ingestion-database-deployment) steps below before triggering the first deployment.

### 5. Trigger first deployment

```bash
doctl apps create-deployment $APP_ID
```

### 6. Watch logs

```bash
doctl apps logs $APP_ID --follow
```

A successful startup looks like:

```
Downloading ChromaDB from s3://<bucket>/chroma-db/ (https://fra1.digitaloceanspaces.com)
  chroma-db/chroma.sqlite3 → .../app/db/chroma.sqlite3
  chroma-db/413056d6-.../header.bin → ...
  ...
Downloaded N file(s) to .../app/db
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080
```

---

## Ingestion Database Deployment

Run this procedure whenever the source texts change (files added, removed, or edited in `texts/`) or after changing the embedding model.

Ingestion uses a local ONNX model — no OpenAI API key is required.

### 1. Run ingestion locally

```bash
uv run python -m ingestion.main
# Equivalent shorthand via mise:
mise run ingest
```

This populates `app/db/` with `chroma.sqlite3` and HNSW segment directories. The `app/db/` directory is git-ignored and will not be committed.

### 2. Upload to Spaces

```bash
aws s3 sync app/db/ s3://<your-bucket-name>/chroma-db/ \
  --endpoint-url https://fra1.digitaloceanspaces.com \
  --delete
```

`--delete` removes stale segment directories from previous ingestion runs, keeping the bucket consistent with the current local state.

### 3. Redeploy the app

```bash
doctl apps create-deployment $APP_ID
```

The new container downloads the updated ChromaDB from Spaces at startup and begins serving traffic.

---

## Code-Only Deploys

Pushing to `main` triggers an automatic redeploy via the `deploy_on_push: true` setting in `.do/app.yaml`. No manual action is needed.

Each new container re-downloads the ChromaDB from Spaces (whatever version is currently in the bucket) before starting uvicorn.

---

## Checking Logs

```bash
# Live log stream
doctl apps logs $APP_ID --follow

# Logs for a specific deployment
doctl apps logs $APP_ID --deployment <deployment-id>

# List recent deployments with their IDs and status
doctl apps list-deployments $APP_ID
```

---

## Rollback

```bash
# Find the previous good deployment
doctl apps list-deployments $APP_ID

# Re-run it
doctl apps create-deployment $APP_ID --deployment-id <previous-deployment-id>
```

Note: rolling back code does not roll back the Spaces database. If the database was also updated, re-run the [ingestion deployment](#ingestion-database-deployment) with the previous ingestion output, or restore from a Spaces backup.

---

## Rotating Secrets

### Rotate SESSION_SECRET_KEY (invalidates all active sessions)

```bash
NEW_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

doctl apps update $APP_ID --spec - << EOF
name: inner-light-quest
region: fra
services:
  - name: web
    envs:
      - key: SESSION_SECRET_KEY
        value: "$NEW_KEY"
        type: SECRET
EOF

doctl apps create-deployment $APP_ID
```

### Rotate OPENAI_API_KEY or Spaces credentials

Use the same pattern as above, substituting the relevant `key` name and new value.
