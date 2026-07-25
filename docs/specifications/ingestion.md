# Document Ingestion Pipeline Specification (CocoIndex → ChromaDB)

Status: draft specification, not yet implemented.

## 1. Purpose

This project currently ships a pre-built vector store (`app/db/chroma.sqlite3`) with no
checked-in code that produced it. `texts/` contains eight George Fox source texts that
are presumably the origin of that data, but there is no repeatable way to rebuild or
update the index. This document specifies an ingestion pipeline, built on
[CocoIndex](https://cocoindex.io/), that reads `texts/`, chunks and embeds the content,
and writes it into the same ChromaDB collection that [`app/vector_store.py`](../../app/vector_store.py)
queries at request time — so the two stay schema-compatible.

## 2. Important finding: CocoIndex has no first-party ChromaDB target

This spec was written against **CocoIndex 1.0.x**, the current PyPI release, whose public
Python API is the `cocoindex` (`coco`) module described at `cocoindex.io/docs` (not the
older, now-superseded `@cocoindex.flow_def`/`DataScope` API documented under
`cocoindex.io/docs-v0`, which belonged to the pre-1.0 releases).

Checking the current connector list (`cocoindex.io/docs/connectors`) directly against the
docs source tree confirms the built-in vector-store targets are **LanceDB, Qdrant,
Turbopuffer, Valkey, and zvec** — ChromaDB is not among them. An earlier ChromaDB target
existed in the pre-1.0 `0.3.x` line (changelog `0.3.27–0.3.34`) but does not appear to
have been carried into the 1.0 rewrite.

This means we cannot use a `declare_row()`/`TableTarget`-style built-in connector the way
the canonical CocoIndex examples do for Postgres/LanceDB/Qdrant. Two ways forward:

- **(A) Custom target connector** — implement CocoIndex's `TargetHandler` protocol
  (`reconcile()`, tracking records, etc., see `advanced_topics/custom_target_connector`)
  so ChromaDB gets full declarative create/update/delete semantics. This is the "proper"
  long-term approach but is substantial machinery for an 8-file, rarely-changing corpus.
- **(B) Plain memoized function + manual reconciliation** — CocoIndex's own docs
  recommend this for simple cases ("Start Simple" tip in the custom-target-connector
  guide): use `@coco.fn(memo=True)` for the expensive part (chunking + embedding) and
  write directly to Chroma with a small hand-rolled diff/delete step.

**This spec chooses (B)**, given the corpus size and update cadence, and documents the
one gap it leaves open (see §7). If the ingestion pipeline grows beyond this corpus (more
sources, more frequent updates, multiple consumers), (A) should be revisited.

## 3. Target schema contract

The pipeline must produce data that `ChromaDBStore.query()` can consume unchanged. From
[`app/vector_store.py`](../../app/vector_store.py) and [`app/models.py`](../../app/models.py):

| Chroma field | Requirement |
|---|---|
| collection | Same path/collection as the app: `app/db/`, collection name from `os.getenv("CHROMA_COLLECTION_NAME", "prompt_engineering")` today (see open question in §9 about this name) |
| `ids` | Any string, stable across reruns for unchanged content (see §5.3) |
| `documents` | Chunk text — read back as `VectorStoreResult.content` |
| `metadatas["source"]` | Required — read back as `VectorStoreMetadata.source`; falls back to `f"document_{i}"` if absent, so it must always be set |
| embeddings | 1536-dim vectors from OpenAI `text-embedding-3-small`, matching the `OpenAIEmbeddingFunction(model_name="text-embedding-3-small")` the app's collection is configured with |

Note `VectorStoreMetadata.score` is **not** written at ingestion time — `ChromaDBStore`
computes it from query-time cosine distance. The ingestion pipeline never sets a score.

## 4. Source data

```
texts/
  doctrinal_works_vol_I.txt          1.1 MB
  doctrinal_works_vol_II.txt         1.3 MB
  doctrinal_works_vol_III.txt        1.4 MB
  epistles_of_george_fox_vol_I.txt   0.9 MB
  epistles_of_george_fox_vol_II.txt  0.8 MB
  selections_from_the_journal_of_george_fox.txt   0.1 MB
  the_great_mystery_of_the_great_whore.text       1.7 MB  ← note: .text, not .txt
  the_journal_of_george_fox.txt      2.5 MB
```

All are plain-prose text files (not Markdown/code) — paragraph-separated, no
headings/structure CocoIndex's syntax-aware splitter can use. The path matcher must
explicitly include both `**/*.txt` and `**/*.text` or the largest single file
(`the_great_mystery_of_the_great_whore.text`, 1.7 MB) is silently skipped.

## 5. Pipeline design

### 5.1 Flow

`texts/` is the canonical folder this pipeline watches. It must both do an initial
catch-up ingest of whatever's already there and keep running to pick up files added or
modified afterward — driven by `mise run ingest` (§5.5), which runs CocoIndex in
`--live` mode.

```
texts/*.txt, *.text  (localfs.walk_dir, live=True)
        │
        │  one processing component PER FILE (coco.mount_each), live-watched:
        │  re-triggered automatically when a file is added or its content changes
        ▼
  read full file text                         [process_file, not memoized]
        │
        ▼
  split into overlapping chunks                [SeparatorSplitter — see §5.2]
        │
        ▼
  embed each chunk (OpenAI text-embedding-3-small via LiteLLM)  [embed_chunk, memo=True]
        │
        ▼
  diff against this file's existing Chroma ids (by `source`); upsert changed/new,
  delete this file's stale ids                 [end of process_file, scoped to one source]
```

This is a per-file design, not the single corpus-wide pass an earlier draft of this spec
used: live mode's incremental re-triggering (`localfs.walk_dir(..., live=True)` combined
with `coco.mount_each()`) works by mounting, updating, and removing **one processing
component per file** as the source directory changes — there's no single "end of run"
moment across the whole corpus to hang a corpus-wide diff off of once the pipeline is
watching continuously. So each file's Chroma reconciliation (diff by `metadatas["source"]
== this file"`, upsert current chunk ids, delete stale ones) has to happen inside that
file's own `process_file` call, scoped to just that file's rows.

This still fully handles **added** and **modified** files (the scope the user asked for):
a new file gets its own component and its chunks get upserted; a modified file's
component reruns (memoization misses because its content changed), recomputes its chunk
set, and the diff drops whichever chunk ids no longer appear. It does **not** by itself
handle a file being **deleted** from `texts/` — see §7.

### 5.2 Chunking

Use `cocoindex.ops.text.SeparatorSplitter`, not `RecursiveSplitter` — `RecursiveSplitter`'s
syntax-aware mode only covers Markdown and program-language grammars (see the language
table in CocoIndex's text-ops docs); there is no plain-prose/`.txt` entry, so passing an
unsupported `language=` value is a live risk. `SeparatorSplitter` splits on a caller-supplied
regex, which is a good match for prose:

```python
chunks = splitter.split(
    text,
    chunk_size=1500,
    chunk_overlap=300,
    separators=[r"\n\s*\n", r"(?<=[.!?])\s+"],  # paragraph breaks, then sentence breaks
)
```

`chunk_size`/`chunk_overlap` (in characters) are starting points, not verified against
retrieval quality — tune empirically once the pipeline runs (see §10).

### 5.3 Chunk IDs

Use a content hash, not a position offset, so IDs are robust to whatever `Chunk` fields
`SeparatorSplitter` does or doesn't populate (only `RecursiveSplitter` is explicitly
documented as returning position-tracked `Chunk.start`/`Chunk.end`; verify before relying
on those fields for `SeparatorSplitter` output):

```python
chunk_id = f"{relative_source_path}::{hashlib.sha1(chunk.text.encode()).hexdigest()[:16]}"
```

This makes reruns idempotent: unchanged chunks get the same id and Chroma's `upsert`
is a no-op in effect; changed text produces a new id, and the old id falls out of the
diff and gets deleted.

### 5.4 Embedding

Use `cocoindex.ops.litellm.LiteLLMEmbedder("text-embedding-3-small")` — LiteLLM is
CocoIndex's supported path to OpenAI embeddings in the current API (requires
`pip install cocoindex[litellm]`). This reuses the same `OPENAI_API_KEY` env var and the
same model string (`text-embedding-3-small`) already used by
[`app/vector_store.py`](../../app/vector_store.py) and [`app/chat_gpt_client.py`](../../app/chat_gpt_client.py),
so query-time and ingestion-time embeddings are directly comparable.

Wrap the embed call in a memoized `@coco.fn` — CocoIndex's own guidance flags embedding
as the textbook case for `memo=True` (expensive call, small fixed-size return value), so
unchanged chunks are never re-embedded on subsequent `cocoindex update` runs.

### 5.5 Running it: the `ingest` Mise task

`mise.toml` defines:

```toml
[tasks.ingest]
description = "Ingest texts/ into Chroma: scan existing files once, then keep watching for added/modified files"
run = "uv run cocoindex update ingestion/main.py --live"

[tasks."ingest:once"]
description = "Ingest texts/ into Chroma once and exit (no watching) — for CI or a manual one-off rebuild"
run = "uv run cocoindex update ingestion/main.py"
```

`mise run ingest` is the day-to-day command: `cocoindex update ... --live` always does a
full catch-up pass first — every existing file in `texts/` gets its own processing
component and is ingested — and then, because the source connector is opened with
`localfs.walk_dir(sourcedir, live=True, ...)` (§6), it keeps the process running and
reacts to files being added or modified without needing a restart. `mise run ingest:once`
runs the same catch-up pass and exits, which is what CI or a manual rebuild wants instead
of a long-running watcher.

## 6. Illustrative code

This is a design sketch, not tested/reviewed implementation. It follows the verified
CocoIndex 1.0 API surface (`ContextKey`, `@coco.lifespan`, `@coco.fn`, `coco.map`,
`localfs.walk_dir`) but should be treated as a starting point.

```python
# ingestion/main.py
from __future__ import annotations

import hashlib
import os
import pathlib
from typing import AsyncIterator

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

import cocoindex as coco
from cocoindex.connectors import localfs
from cocoindex.resources.file import FileLike, PatternFilePathMatcher
from cocoindex.ops.text import SeparatorSplitter
from cocoindex.ops.litellm import LiteLLMEmbedder

load_dotenv()

CHROMA_DB_PATH = pathlib.Path(__file__).resolve().parent.parent / "app" / "db"
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "prompt_engineering")
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 300

CHROMA_CLIENT = coco.ContextKey[chromadb.ClientAPI]("chroma_client")
EMBEDDER = coco.ContextKey[LiteLLMEmbedder]("embedder", detect_change=True)

_splitter = SeparatorSplitter()


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    client = chromadb.PersistentClient(
        path=str(CHROMA_DB_PATH), settings=Settings(allow_reset=True)
    )
    builder.provide(CHROMA_CLIENT, client)
    builder.provide(EMBEDDER, LiteLLMEmbedder("text-embedding-3-small"))
    yield


def make_chunk_id(source: str, chunk_text: str) -> str:
    digest = hashlib.sha1(chunk_text.encode()).hexdigest()[:16]
    return f"{source}::{digest}"


@coco.fn(memo=True)
async def embed_chunk(text: str) -> list[float]:
    vec = await coco.use_context(EMBEDDER).embed(text)
    return vec.tolist()


@coco.fn
async def process_file(file: FileLike) -> None:
    """One processing component per source file (mounted by app_main via
    mount_each). Live mode re-invokes this only for files that are new or whose
    content changed. Deliberately NOT memoized, unlike embed_chunk: this function
    ends in a Chroma upsert/delete side effect, and memo=True would skip that
    body (and thus the write) on an exact repeat of a prior run's input — e.g.
    after a manual `client.delete_collection()` (§9 open question 2) with the
    CocoIndex memo cache still warm, silently leaving Chroma without those rows.
    The chunking/embedding work is comparatively cheap and re-running it is safe;
    only embed_chunk carries memo=True, since it's the one call worth skipping on
    a cache hit (avoids a paid, network-bound OpenAI call) and it has no side
    effects of its own."""
    text = await file.read_text()
    source = str(file.file_path.path)

    chunks = _splitter.split(
        text,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=[r"\n\s*\n", r"(?<=[.!?])\s+"],
    )
    desired_ids = [make_chunk_id(source, c.text) for c in chunks]
    embeddings = await coco.map(embed_chunk, [c.text for c in chunks])

    client = coco.use_context(CHROMA_CLIENT)
    collection = client.get_or_create_collection(COLLECTION_NAME)

    # Reconcile only THIS file's rows — diff by `source`, not the whole collection.
    existing = collection.get(where={"source": source}, include=[])
    stale_ids = set(existing["ids"]) - set(desired_ids)

    # Upsert before delete: if upsert raises, the stale rows are still in place
    # instead of having already been removed ahead of a write that didn't land.
    if desired_ids:
        collection.upsert(
            ids=desired_ids,
            documents=[c.text for c in chunks],
            metadatas=[{"source": source}] * len(chunks),
            embeddings=embeddings,
        )

    if stale_ids:
        collection.delete(ids=list(stale_ids))


@coco.fn
async def app_main(sourcedir: pathlib.Path) -> None:
    matcher = PatternFilePathMatcher(included_patterns=["**/*.txt", "**/*.text"])
    # live=True: after the initial catch-up scan, keep watching sourcedir and
    # mount/re-mount per-file components as files are added or modified.
    files = localfs.walk_dir(sourcedir, recursive=True, path_matcher=matcher, live=True)
    await coco.mount_each(process_file, files.items())


app = coco.App(
    "GeorgeFoxIngestion",
    app_main,
    sourcedir=pathlib.Path(__file__).resolve().parent.parent / "texts",
)
```

Run with `mise run ingest` (§5.5) — `cocoindex update ingestion/main.py --live` is what
actually keeps the process alive to honor `live=True` above; without `--live` it still
does the full catch-up pass and then exits.

**Failure handling for the raw `collection.upsert`/`collection.delete` calls.** Neither
call is wrapped in retry logic — an exception (e.g. a transient network error talking to
the embedded Chroma) propagates out of `process_file` and fails that file's component for
the current run. Recovery is a rerun, not a manual patch: `desired_ids` are content-hashed
(§5.3), so `collection.upsert` is idempotent and a rerun reproduces the same rows. Given
the upsert-before-delete ordering above, a failure has two possible shapes:

- **`upsert` raises** — nothing changed yet; the file's existing (now-stale-by-content)
  rows are untouched. A rerun retries the same upsert.
- **`delete` raises** — the new rows already landed; only the stale ids remain
  (temporarily duplicated content for this source, not lost data). A rerun recomputes the
  same `stale_ids` and retries the delete.

## 7. Known limitations

Because this design writes to Chroma with raw client calls instead of CocoIndex
`declare_*` target states, CocoIndex's own tracking/CLI integration doesn't know about
these writes:

- `cocoindex drop ingestion/main.py` will **not** undo them (there's nothing declared to
  revert). A manual reset means calling `client.delete_collection(COLLECTION_NAME)` (or
  `collection.delete(ids=collection.get(include=[])["ids"])`) directly.
- `cocoindex show`/`cocoindex ls` won't list Chroma rows as managed state.

**Deleting a file from `texts/` does not remove its chunks from Chroma.** With the
per-file design in §5.1/§6, reconciliation only ever runs *inside* `process_file` for a
file that still exists and was mounted — when a file disappears, `mount_each`'s live
watcher simply stops mounting a component for that path; nothing ever runs a diff for the
`source` that used to belong to it, so its rows are orphaned. (This is different from an
earlier draft of this spec, which used a single corpus-wide diff at the end of one big
run specifically to close this gap — that shape doesn't fit continuous live watching,
since there's no "end of run" to hang it off.)

Given the scope asked for here — pick up files that are added or modified — this is an
accepted gap, not a blocker. `texts/` is a small, curated set of historical volumes that's
expected to grow, not shrink; the practical risk is low. If a file removal ever does need
to be reflected, the fix is a separate one-shot maintenance script: walk `texts/` for the
current set of source paths, `collection.get(include=["metadatas"])` for every distinct
`source` currently in Chroma, and delete rows whose `source` has no matching file. That's
deliberately not built here — flag if you want it added as an `ingest:reconcile` Mise
task.

## 8. Configuration

| Env var | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | Already required by the app; reused for embeddings | — |
| `COCOINDEX_DB` | CocoIndex's own internal state DB (memoization cache), separate from `app/db` | e.g. `./ingestion/cocoindex.db` |
| `CHROMA_COLLECTION_NAME` | New — lets ingestion and the app agree on collection name without hardcoding it twice | `prompt_engineering` |

## 9. Open questions for the user

1. **Collection name.** `app/main.py:31` hardcodes `collection_name="prompt_engineering"`,
   which reads like a leftover from a different project template, not something
   George-Fox-specific. Do you want to rename it (e.g. `"george_fox_writings"`) as part
   of standing up this pipeline, or keep it as-is for now? This spec assumes it stays a
   shared constant either way, sourced from `CHROMA_COLLECTION_NAME`.
2. **Existing `app/db/chroma.sqlite3`.** It's unclear whether it was built from exactly
   these eight `texts/` files. With the per-file reconciliation in §5.1/§6, the first run
   will clean up stale rows *for sources that match a current file* (different chunking →
   different ids → old ids fall out of that file's diff) — but any pre-existing rows whose
   `source` doesn't match a file in `texts/` at all are never touched, per the gap noted
   in §7. Recommend a clean `client.delete_collection(COLLECTION_NAME)` before the first
   run rather than trying to reconcile against unknown pre-existing content.
3. **Chunk size/overlap.** 1500/300 chars is a starting guess, not derived from this
   app's retrieval behavior (`top_k=5` in `app/rag_service.py`). Worth a manual pass of
   spot-checking retrieved chunks against real questions before treating as final.

## 10. Validation plan

1. Point `sourcedir` at a temp directory with just one small file
   (`selections_from_the_journal_of_george_fox.txt`, ~100 KB) before the full corpus, to
   sanity-check chunk counts and a few embeddings.
2. Run `mise run ingest:once` twice in a row with no source changes; confirm the second
   run embeds nothing new (memoization hit) and Chroma's row count is unchanged.
3. Modify one source file, run `mise run ingest:once` again; confirm only that file's
   chunks change in Chroma (spot-check via `collection.get(where={"source": "<file>"})`).
4. Start `mise run ingest` (live mode) and, while it's running, add a new `.txt` file to
   `texts/` and separately edit an existing one; confirm both get ingested without
   restarting the process — this is the behavior the live watcher exists for and isn't
   exercised by 1–3 above.
5. Query the resulting collection through the app's existing `ChromaDBStore.query()` /
   `RAGService.get_relevant_context()` path with a few representative questions and
   confirm citations resolve to the expected `source` filenames.

## 11. Dependencies to add

The project has both a legacy `requirements.txt` and a `pyproject.toml`/`uv`-managed
dependency set (see `mise.toml`'s `uv` tool). Add CocoIndex as its own group, mirroring
the existing `dev` group in `pyproject.toml`, rather than folding it into the app's main
runtime dependencies — this is an offline/build-time tool, not part of the FastAPI
runtime:

```toml
[dependency-groups]
ingestion = [
    "cocoindex[litellm]",
]
```

installed via `uv sync` (or `uv add --group ingestion "cocoindex[litellm]"` to add it).
(`chromadb` and `python-dotenv` are already present in both dependency files.)
