"""Download the ChromaDB vector store and ONNX embedding model from DigitalOcean Spaces at container startup.

ChromaDB persistent storage consists of two parts that are both required for
vector similarity search:

  - chroma.sqlite3          — collection metadata and raw embeddings
  - <uuid>/ directories    — HNSW vector index files (header.bin, data_level0.bin, …)

The ONNX MiniLM embedding model (used by DefaultEmbeddingFunction) must also be
present before the first query.  ChromaDB normally downloads it on demand, but the
download mechanism is unreliable in restricted container environments, so we fetch
it from Spaces instead and place it in the path chromadb expects:
  $HOME/.cache/chroma/onnx_models/all-MiniLM-L6-v2/

This script syncs both assets from a Spaces bucket (S3-compatible) before the
application starts, preserving the subdirectory structure.

Required environment variables:
  SPACES_KEY       — Spaces access key ID
  SPACES_SECRET    — Spaces secret access key
  SPACES_BUCKET    — Spaces bucket name

Optional environment variables (with defaults):
  SPACES_REGION      — Spaces region slug (default: fra1)
  SPACES_DB_PREFIX   — Object key prefix for the database (default: chroma-db/)
  SPACES_ONNX_PREFIX — Object key prefix for the ONNX model (default: onnx-model/all-MiniLM-L6-v2/)

Exits non-zero on failure so the Procfile chain stops before uvicorn starts
with an empty or corrupt database.
"""

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Resolves to <project_root>/app/db regardless of the working directory.
DB_DIR = Path(__file__).resolve().parent.parent / "app" / "db"

# chromadb looks for the ONNX model at $HOME/.cache/chroma/onnx_models/all-MiniLM-L6-v2/
ONNX_DIR = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"


def _sync_prefix(client, bucket: str, prefix: str, dest_dir: Path, label: str) -> int:
    """Download all objects under *prefix* from *bucket* into *dest_dir*.
    Returns the number of files downloaded."""
    paginator = client.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            relative = key[len(prefix):]
            if not relative:
                continue
            local_path = dest_dir / relative
            local_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"  {label}: {key} → {local_path}")
            client.download_file(bucket, key, str(local_path))
            count += 1
    return count


def download_db() -> None:
    region = os.environ.get("SPACES_REGION", "fra1")
    db_prefix = os.environ.get("SPACES_DB_PREFIX", "chroma-db/")
    onnx_prefix = os.environ.get("SPACES_ONNX_PREFIX", "onnx-model/all-MiniLM-L6-v2/")
    bucket = os.environ["SPACES_BUCKET"]
    key_id = os.environ["SPACES_KEY"]
    secret = os.environ["SPACES_SECRET"]

    endpoint = f"https://{region}.digitaloceanspaces.com"

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=region,
    )

    print(f"Downloading ChromaDB from s3://{bucket}/{db_prefix} ({endpoint})")
    db_count = _sync_prefix(client, bucket, db_prefix, DB_DIR, "db")

    if db_count == 0:
        print(
            f"ERROR: No objects found at s3://{bucket}/{db_prefix}. "
            "Run ingestion locally and upload the database to Spaces before deploying. "
            "See docs/how-to/deploy.md for instructions.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Downloaded {db_count} ChromaDB file(s) to {DB_DIR}")

    print(f"Downloading ONNX model from s3://{bucket}/{onnx_prefix}")
    onnx_count = _sync_prefix(client, bucket, onnx_prefix, ONNX_DIR, "onnx")

    if onnx_count == 0:
        print(
            f"ERROR: No ONNX model files found at s3://{bucket}/{onnx_prefix}. "
            "Upload the model with: scripts/upload_onnx_model.py",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Downloaded {onnx_count} ONNX model file(s) to {ONNX_DIR}")


if __name__ == "__main__":
    try:
        download_db()
    except KeyError as exc:
        print(f"ERROR: Missing required environment variable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: Spaces download failed: {exc}", file=sys.stderr)
        sys.exit(1)
