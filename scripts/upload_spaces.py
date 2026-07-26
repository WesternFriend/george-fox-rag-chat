"""Upload ChromaDB and/or ONNX model files to DigitalOcean Spaces.

Usage (via mise):
  mise run upload-db      # after ingestion
  mise run upload-onnx    # after bumping chromadb version
  mise run upload-all     # both

Usage (direct):
  uv run python scripts/upload_spaces.py db
  uv run python scripts/upload_spaces.py onnx
  uv run python scripts/upload_spaces.py all

Required environment variables (loaded from .env by mise, or set manually):
  SPACES_KEY, SPACES_SECRET, SPACES_BUCKET

Optional (with defaults):
  SPACES_REGION      (default: fra1)
  SPACES_DB_PREFIX   (default: chroma-db/)
  SPACES_ONNX_PREFIX (default: onnx-model/all-MiniLM-L6-v2/)

When to run upload-onnx:
  After upgrading the chromadb dependency in pyproject.toml. The ONNX model
  binary is embedded in the chromadb package; a version bump may change the
  expected model file. Let chromadb download the new model locally first by
  running a query (e.g. mise run ingest), then run upload-onnx.
"""

import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DB_DIR = Path(__file__).resolve().parent.parent / "app" / "db"
ONNX_DIR = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"


def _make_client():
    region = os.environ.get("SPACES_REGION", "fra1")
    bucket = os.environ["SPACES_BUCKET"]
    return (
        boto3.client(
            "s3",
            region_name=region,
            endpoint_url=f"https://{region}.digitaloceanspaces.com",
            aws_access_key_id=os.environ["SPACES_KEY"],
            aws_secret_access_key=os.environ["SPACES_SECRET"],
        ),
        bucket,
        region,
    )


def _upload_dir(client, bucket: str, local_dir: Path, prefix: str, label: str) -> int:
    files = sorted([f for f in local_dir.rglob("*") if f.is_file()])
    if not files:
        print(f"ERROR: No files found in {local_dir}", file=sys.stderr)
        return 0
    total_mb = sum(f.stat().st_size for f in files) / 1e6
    print(f"Uploading {label}: {len(files)} files ({total_mb:.1f} MB) → s3://{bucket}/{prefix}")
    for i, path in enumerate(files, 1):
        key = prefix + str(path.relative_to(local_dir))
        print(f"  [{i}/{len(files)}] {path.name}  ({path.stat().st_size / 1e6:.1f} MB)")
        client.upload_file(str(path), bucket, key)
    return len(files)


def upload_db():
    client, bucket, _ = _make_client()
    prefix = os.environ.get("SPACES_DB_PREFIX", "chroma-db/")
    if not DB_DIR.exists():
        print(f"ERROR: {DB_DIR} does not exist. Run 'mise run ingest' first.", file=sys.stderr)
        sys.exit(1)
    count = _upload_dir(client, bucket, DB_DIR, prefix, "ChromaDB")
    if count == 0:
        sys.exit(1)
    print(f"ChromaDB upload complete ({count} files).")


def upload_onnx():
    client, bucket, _ = _make_client()
    prefix = os.environ.get("SPACES_ONNX_PREFIX", "onnx-model/all-MiniLM-L6-v2/")
    if not ONNX_DIR.exists():
        print(
            f"ERROR: {ONNX_DIR} does not exist.\n"
            "chromadb downloads the ONNX model on first use. Run a query locally\n"
            "(e.g. 'mise run ingest') to populate the cache, then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)
    count = _upload_dir(client, bucket, ONNX_DIR, prefix, "ONNX model")
    if count == 0:
        sys.exit(1)
    print(f"ONNX model upload complete ({count} files).")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("db", "onnx", "all"):
        print("Usage: upload_spaces.py [db|onnx|all]", file=sys.stderr)
        sys.exit(1)

    try:
        cmd = sys.argv[1]
        if cmd in ("db", "all"):
            upload_db()
        if cmd in ("onnx", "all"):
            upload_onnx()
    except KeyError as exc:
        print(f"ERROR: Missing environment variable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: Spaces upload failed: {exc}", file=sys.stderr)
        sys.exit(1)
