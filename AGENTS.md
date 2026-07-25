# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A RAG (Retrieval-Augmented Generation) chat application that answers questions about George Fox's writings and Quaker history/practice. FastAPI backend, HTMX + Bootstrap frontend (server-rendered partials, no JS framework), OpenAI for embeddings and chat completion, ChromaDB for vector storage.

## Commands

```bash
# Setup
uv sync
cp .env.example .env   # then set OPENAI_API_KEY and SESSION_SECRET_KEY
npm install            # frontend (vitest) test tooling

# Run the dev server (note: module path is app.main, not chat.py despite what README says)
# ENVIRONMENT=development in .env disables the Secure cookie flag for plain-HTTP localhost
uv run uvicorn app.main:app --reload
mise run dev                                # equivalent, via mise.toml

# Backend tests
uv run pytest
uv run pytest --cov=app                     # with coverage
uv run pytest tests/test_main.py            # single file
uv run pytest tests/test_main.py::test_sources_toggle_independent   # single test

# Frontend tests (app/static/tts.js state machine)
npm test
```

There's also a VSCode launch config ("Python Debugger: FastAPI") that runs `uvicorn app.main:app --reload`.

No lint/format tooling is configured in this repo (no ruff/black/flake8 config present).

## Architecture

Request flow: `app/main.py` (FastAPI routes) → `app/session.py` (per-visitor `SessionState`) → `app/rag_service.py` (RAGService) → `app/vector_store.py` (ChromaDBStore) for retrieval, then → `app/chat_gpt_client.py` for the completion call. Bot responses can optionally be read aloud via `app/tts_service.py` → `app/tts_client.py` → OpenAI's speech endpoint.

- **`app/main.py`** — FastAPI app. Routes depend on `Depends(get_session)` (`app/session.py`) to reach the caller's own `SessionState` — there is no global chat history anymore. `POST /chat` is the core endpoint: it builds RAG-augmented messages, calls OpenAI, renders the Markdown response to sanitized HTML (`markdown2.markdown(..., safe_mode="escape")`), computes `tts_available` (§4.2 of the TTS spec) and `response_truncated`, appends the exchange to the caller's session, and returns an HTML partial (`bot_message.html`) for HTMX to swap in. The system prompt lives inline in this file as `SYSTEM_PROMPT` (with `AUDIO_MODE_ADDENDUM` appended when `audio_mode` is set). `POST /api/messages/{message_id}/speech` synthesizes/serves cached audio for a given bot message, session-scoped. `POST /chat` and the speech route are both rate-limited (`slowapi`, keyed by session id). A `session_cookie_middleware` sets/refreshes the session cookie on every response, since FastAPI won't merge an injected `Response` dependency's headers into a route's own returned `TemplateResponse`.
- **`app/session.py`** — Session identity (`itsdangerous`-signed opaque cookie, no payload beyond the id) and the in-memory `SessionStore` (TTL sliding expiration, per-session message cap, LRU eviction across sessions). `get_session` is the FastAPI dependency routes use to reach their `SessionState`. See `docs/specifications/session_isolation.md`.
- **`app/tts_client.py`** — Thin wrapper around `client.audio.speech.with_streaming_response.create(...)`, returning raw MP3 bytes. No caching/ownership logic here.
- **`app/tts_service.py`** — `TTSService`: caches synthesized audio keyed by `(session_id, message_id)`, with a per-key lock (dedupes concurrent requests for the same message), a global semaphore (bounds concurrent upstream OpenAI calls), and count+byte-bounded eviction. See `docs/specifications/text_to_speech.md`.
- **`app/rag_service.py`** — `RAGService.prepare_messages_with_sources()` is the main entry point: queries the vector store for context, then assembles `[system+context, ...recent history, user message]` plus a parallel list of `RagCitation`s for display. `prepare_messages()` (no citations) is kept only for backwards compatibility — prefer the `_with_sources` variant for new code.
- **`app/vector_store.py`** — Defines the `VectorStore` ABC (`async query(query, top_k) -> List[VectorStoreResult]`) with several implementations: `ChromaDBStore` (the one actually wired up in `main.py`, backed by `app/db/chroma.sqlite3` using OpenAI's `text-embedding-3-small` embedding function), `MockVectorStore` (random lorem-ipsum results, useful for tests/dev without hitting OpenAI), and unimplemented placeholders (`PineconeStore`, `AstraDBStore`) for future backends. When adding a new backend, implement this ABC.
- **`app/chat_gpt_client.py`** — Thin async wrapper around the OpenAI chat completions API. `Message`/`MessageRole` (pydantic model / enum) are the shared message representation used throughout the app (main, rag_service, tests); `Message` also carries `id` and `tts_available`. `get_chat_response_with_history` returns a `ChatCompletionResult(content, finish_reason)` NamedTuple, not a bare string — callers need `finish_reason` to detect a `max_tokens`-truncated response. Model/temperature/max_tokens default from `CHAT_GPT_MODEL`, `CHAT_GPT_TEMPERATURE`, `CHAT_GPT_MAX_TOKENS`/`CHAT_GPT_MAX_TOKENS_AUDIO` env vars.
- **`app/langflow_client.py`** — An alternate/experimental integration path (calls a local Langflow server via HTTP) that is **not** currently wired into `main.py`. Treat as unused/legacy unless a task specifically involves Langflow.
- **`app/db/`** — Pre-built ChromaDB SQLite store (`chroma.sqlite3`). There is no ingestion/embedding script checked into the repo — the `texts/` directory (George Fox source texts) is the presumed origin of the embedded content, but the pipeline that built `app/db` from `texts/` isn't part of this codebase currently.
- **`app/static/tts.js`** — ES module, side-effect-free on import (so it's directly importable from `vitest`/jsdom). Exports `TTSController` (the idle/loading/playing/error state machine per message id) and `initTTSController()`, which wires it to the live page via event delegation on `#chat-container` — called from a small inline `<script type="module">` in `chat.html`, not from `tts.js` itself.
- **`tests/`** — Uses `TestClient` + `monkeypatch` to stub `app.main.rag_service.prepare_messages_with_sources` and `app.main.get_chat_response_with_history`, so tests don't hit OpenAI or ChromaDB. `test_main.py` parses returned HTML with BeautifulSoup to assert on HTMX/Bootstrap collapse-toggle behavior (each bot message's citation panel needs a unique DOM id) and uses `TestClient`'s cookie jar to simulate independent sessions. `tests/frontend/` (vitest) covers `tts.js`'s state machine directly, independent of the FastAPI app.

## Deployment

`Procfile` runs `uvicorn app.main:app --host=0.0.0.0 --port=${PORT:-8000}` (Heroku-style).
