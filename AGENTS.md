# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A RAG (Retrieval-Augmented Generation) chat application that answers questions about George Fox's writings and Quaker history/practice. FastAPI backend, HTMX + Bootstrap frontend (server-rendered partials, no JS framework), OpenAI for embeddings and chat completion, ChromaDB for vector storage.

## Commands

```bash
# Setup
uv sync
cp .env.example .env   # then set OPENAI_API_KEY

# Run the dev server (note: module path is app.main, not chat.py despite what README says)
uv run uvicorn app.main:app --reload

# Tests
uv run pytest
uv run pytest --cov=app                     # with coverage
uv run pytest tests/test_main.py            # single file
uv run pytest tests/test_main.py::test_sources_toggle_independent   # single test
```

There's also a VSCode launch config ("Python Debugger: FastAPI") that runs `uvicorn app.main:app --reload`.

No lint/format tooling is configured in this repo (no ruff/black/flake8 config present).

## Architecture

Request flow: `app/main.py` (FastAPI routes) → `app/rag_service.py` (RAGService) → `app/vector_store.py` (ChromaDBStore) for retrieval, then → `app/chat_gpt_client.py` for the completion call.

- **`app/main.py`** — FastAPI app. Chat history is an in-memory list (`chat_history: List[Message]`), not persisted — restarting the server clears it. `POST /chat` is the core endpoint: it builds RAG-augmented messages, calls OpenAI, renders the Markdown response to sanitized HTML (`markdown2.markdown(..., safe_mode="escape")`), and returns an HTML partial (`bot_message.html`) for HTMX to swap in. The system prompt lives inline in this file as `SYSTEM_PROMPT`.
- **`app/rag_service.py`** — `RAGService.prepare_messages_with_sources()` is the main entry point: queries the vector store for context, then assembles `[system+context, ...recent history, user message]` plus a parallel list of `RagCitation`s for display. `prepare_messages()` (no citations) is kept only for backwards compatibility — prefer the `_with_sources` variant for new code.
- **`app/vector_store.py`** — Defines the `VectorStore` ABC (`async query(query, top_k) -> List[VectorStoreResult]`) with several implementations: `ChromaDBStore` (the one actually wired up in `main.py`, backed by `app/db/chroma.sqlite3` using OpenAI's `text-embedding-3-small` embedding function), `MockVectorStore` (random lorem-ipsum results, useful for tests/dev without hitting OpenAI), and unimplemented placeholders (`PineconeStore`, `AstraDBStore`) for future backends. When adding a new backend, implement this ABC.
- **`app/chat_gpt_client.py`** — Thin async wrapper around the OpenAI chat completions API. `Message`/`MessageRole` (pydantic model / enum) are the shared message representation used throughout the app (main, rag_service, tests). Model/temperature/max_tokens default from `CHAT_GPT_MODEL`, `CHAT_GPT_TEMPERATURE`, `CHAT_GPT_MAX_TOKENS` env vars.
- **`app/langflow_client.py`** — An alternate/experimental integration path (calls a local Langflow server via HTTP) that is **not** currently wired into `main.py`. Treat as unused/legacy unless a task specifically involves Langflow.
- **`app/db/`** — Pre-built ChromaDB SQLite store (`chroma.sqlite3`). There is no ingestion/embedding script checked into the repo — the `texts/` directory (George Fox source texts) is the presumed origin of the embedded content, but the pipeline that built `app/db` from `texts/` isn't part of this codebase currently.
- **`tests/`** — Uses `TestClient` + `monkeypatch` to stub `app.main.rag_service.prepare_messages_with_sources` and `app.main.get_chat_response_with_history`, so tests don't hit OpenAI or ChromaDB. `test_main.py` parses returned HTML with BeautifulSoup to assert on HTMX/Bootstrap collapse-toggle behavior (each bot message's citation panel needs a unique DOM id).

## Deployment

`Procfile` runs `uvicorn app.main:app --host=0.0.0.0 --port=${PORT:-8000}` (Heroku-style).
