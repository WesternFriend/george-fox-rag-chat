"""Download the ChromaDB vector store from DigitalOcean Spaces at container startup.

ChromaDB persistent storage consists of two parts that are both required for
vector similarity search:

  - chroma.sqlite3          — collection metadata and raw embeddings
  - <uuid>/ directories    — HNSW vector index files (header.bin, data_level0.bin, …)

This script syncs the full app/db/ directory from a Spaces bucket (S3-compatible)
before the application starts, preserving the subdirectory structure.

Required environment variables:
  SPACES_KEY       — Spaces access key ID
  SPACES_SECRET    — Spaces secret access key
  SPACES_BUCKET    — Spaces bucket name

Optional environment variables (with defaults):
  SPACES_REGION    — Spaces region slug (default: fra1)
  SPACES_DB_PREFIX — Object key prefix for the database (default: chroma-db/)

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


def download_db() -> None:
    region = os.environ.get("SPACES_REGION", "fra1")
    prefix = os.environ.get("SPACES_DB_PREFIX", "chroma-db/")
    bucket = os.environ["SPACES_BUCKET"]
    key_id = os.environ["SPACES_KEY"]
    secret = os.environ["SPACES_SECRET"]

    endpoint = f"https://{region}.digitaloceanspaces.com"
    print(f"Downloading ChromaDB from s3://{bucket}/{prefix} ({endpoint})")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=region,
    )

    paginator = client.get_paginator("list_objects_v2")
    count = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            # Strip the prefix to get the path relative to app/db/
            relative = key[len(prefix) :]
            if not relative:
                continue  # skip the prefix placeholder entry itself

            local_path = DB_DIR / relative
            local_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"  {key} → {local_path}")
            client.download_file(bucket, key, str(local_path))
            count += 1

    if count == 0:
        print(
            f"ERROR: No objects found at s3://{bucket}/{prefix}. "
            "Run ingestion locally and upload the database to Spaces before deploying. "
            "See docs/how-to/deploy.md for instructions.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Downloaded {count} file(s) to {DB_DIR}")


if __name__ == "__main__":
    try:
        download_db()
    except KeyError as exc:
        print(f"ERROR: Missing required environment variable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: Spaces download failed: {exc}", file=sys.stderr)
        sys.exit(1)
