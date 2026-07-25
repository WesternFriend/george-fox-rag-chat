"""Ingest texts/ into the Chroma `quaker_texts` collection.

Chunks each source file with langchain-text-splitters, embeds chunks with the
same local ONNX MiniLM model used at query time by app.vector_store.ChromaDBStore,
attaches title/authors/origin metadata from sources.yaml, and reconciles each
file's rows in Chroma (upsert changed/new chunks, delete stale ones) keyed by
content hash so reruns are idempotent.

Run with: uv run python -m ingestion.main
"""

import hashlib
from pathlib import Path

import chromadb
import yaml
from chromadb.api.types import Metadata
from chromadb.config import Settings
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEXTS_DIR = PROJECT_ROOT / "texts"
CHROMA_DB_PATH = PROJECT_ROOT / "app" / "db"
SOURCES_YAML_PATH = TEXTS_DIR / "sources.yaml"
COLLECTION_NAME = "quaker_texts"  # must match app/main.py's ChromaDBStore call

CHUNK_SIZE = 1500
CHUNK_OVERLAP = 300

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
)


def load_source_catalog() -> dict:
    if not SOURCES_YAML_PATH.exists():
        return {}
    with SOURCES_YAML_PATH.open() as f:
        return yaml.safe_load(f) or {}


def make_chunk_id(source: str, chunk_text: str) -> str:
    digest = hashlib.sha1(chunk_text.encode()).hexdigest()[:16]
    return f"{source}::{digest}"


def build_metadata(source: str, source_catalog: dict) -> Metadata:
    metadata: dict[str, str] = {"source": source}
    entry = source_catalog.get(source) or {}
    if entry.get("title"):
        metadata["title"] = entry["title"]
    if entry.get("authors"):
        metadata["authors"] = "; ".join(entry["authors"])
    if entry.get("origin"):
        metadata["origin"] = entry["origin"]
    return metadata


def ingest_file(
    collection: chromadb.Collection, path: Path, source_catalog: dict
) -> tuple[int, int]:
    """Reconcile one file's chunks into the collection. Returns
    (chunks upserted, stale rows deleted)."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    source = path.relative_to(TEXTS_DIR).as_posix()

    chunks = _splitter.split_text(text)
    if not chunks:
        return 0, 0

    # Identical chunk text within a file hashes to the same id; keep one copy
    # since duplicate rows for the same (source, content) would be redundant.
    seen_ids = set()
    deduped_ids = []
    deduped_chunks = []
    for chunk in chunks:
        chunk_id = make_chunk_id(source, chunk)
        if chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)
        deduped_ids.append(chunk_id)
        deduped_chunks.append(chunk)
    desired_ids, chunks = deduped_ids, deduped_chunks
    metadata = build_metadata(source, source_catalog)

    existing = collection.get(where={"source": source}, include=[])
    stale_ids = set(existing["ids"]) - set(desired_ids)

    # Upsert before delete: if upsert raises, the stale rows are still in
    # place instead of having already been removed ahead of a write that
    # didn't land.
    collection.upsert(
        ids=desired_ids,
        documents=chunks,
        metadatas=[metadata] * len(chunks),
    )
    if stale_ids:
        collection.delete(ids=list(stale_ids))

    return len(chunks), len(stale_ids)


def main() -> None:
    source_catalog = load_source_catalog()

    client = chromadb.PersistentClient(
        path=str(CHROMA_DB_PATH), settings=Settings(allow_reset=True)
    )
    local_ef = embedding_functions.DefaultEmbeddingFunction()
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=local_ef,  # type: ignore[arg-type]  # chromadb's own EmbeddingFunction stubs are inconsistent with its built-in implementations
    )

    files = sorted(
        {p for pattern in ("*.txt", "*.text") for p in TEXTS_DIR.glob(pattern)}
    )

    total_chunks = 0
    total_deleted = 0
    for path in files:
        source = path.relative_to(TEXTS_DIR).as_posix()
        if source not in source_catalog:
            print(f"warning: {source} has no entry in sources.yaml")

        n_chunks, n_deleted = ingest_file(collection, path, source_catalog)
        print(f"{path.name}: {n_chunks} chunks upserted, {n_deleted} stale removed")
        total_chunks += n_chunks
        total_deleted += n_deleted

    known_sources = {p.relative_to(TEXTS_DIR).as_posix() for p in files}
    all_metadatas = collection.get(include=["metadatas"])["metadatas"] or []
    db_sources = {str(m["source"]) for m in all_metadatas}
    orphaned_sources = db_sources - known_sources
    if orphaned_sources:
        for source in orphaned_sources:
            orphaned_ids = collection.get(where={"source": source}, include=[])["ids"]
            collection.delete(ids=orphaned_ids)
        print(
            f"Removed rows for {len(orphaned_sources)} source(s) no longer in "
            f"texts/: {sorted(orphaned_sources)}"
        )

    print(
        f"Done. {len(files)} files, {total_chunks} chunks total, "
        f"{total_deleted} stale rows removed. Collection now has "
        f"{collection.count()} rows."
    )


if __name__ == "__main__":
    main()
