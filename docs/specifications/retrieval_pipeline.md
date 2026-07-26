# Retrieval Pipeline Specification (Query Expansion + Reranking)

Status: implemented (`app/query_expansion.py`, `app/reranker.py`, `app/rag_service.py`).

## 1. Problem this solves

Manual tuning of `app/prompts/system_prompt.md`, done via `scripts/prompt_playground.py`
against the real pipeline, found that grounding quality has hit a generation-side ceiling
(see `conversational_orientation.md` §6). Two representative test questions, same `top_k=5`
retrieval:

- **"What does it mean to have 'that of God in everyone'?"** — retrieval returns Fox's
  own Epistles, directly on point. After prompt tuning, the model quotes verbatim
  reliably (4-of-4 trials).
- **"How did George Fox think about discernment?"** — retrieval returns mostly editorial
  preface material and Penn's biographical sketch of Fox's childhood, because Fox's texts
  rarely use the modern word "discernment" (he writes of the Inward Light, leadings,
  waiting, convincement). The model correctly declines to fabricate a quote rather than
  force one — but that means the reply falls back to generic explanation (0-of-3 trials).

No amount of system-prompt wording fixes the second case; the retrieved candidates
themselves aren't good material. This spec adds two stages ahead of generation — query
expansion and reranking — to address it.

## 2. Why not a framework (LangChain/LangGraph)

Considered and rejected for this pipeline specifically (see chat history for the fuller
reasoning). Summary:

- The app's existing pattern everywhere — `chat_gpt_client.py`, `vector_store.py`,
  `tts_client.py` — is a thin, direct wrapper around the vendor SDK plus Pydantic models
  for structure. No chain/agent orchestration layer exists today.
- `langchain-text-splitters` is already a dependency, but it's a narrow chunking utility
  used at ingestion time, not the orchestration framework.
- `app/langflow_client.py` — a hand-rolled HTTP client for a self-hosted Langflow server
  (a visual builder on top of LangChain) — was unused/legacy per `AGENTS.md` and has been
  removed as part of this work: a framework-driven approach was already tried here and
  didn't make it to production.
- This pipeline is four **linear, fixed** stages (expand → retrieve → rerank → generate),
  not dynamic/branching/agentic behavior — the case where a framework's orchestration
  primitives would earn their complexity and dependency cost.
- Plain functions keep the existing test convention intact: `tests/test_main.py` already
  monkeypatches `app.main.rag_service.prepare_messages_with_sources` and
  `get_chat_response_with_history` directly. A LangChain/LCEL implementation would make
  that harder without adopting its own testing idioms.

If the pipeline later needs dynamic branching (e.g. choosing a retrieval strategy at
runtime, multi-hop retrieval, tool-calling loops), revisit **LangGraph** specifically
(explicit state-passing between nodes) rather than classic LangChain chains. Not needed
for what's specified here.

## 3. Structured output mechanism

Both new LLM calls (query expansion, rerank) use OpenAI structured outputs:
`await client.chat.completions.parse(model=..., messages=..., response_format=SomePydanticModel)`.

Verified available and stable (not `.beta`) in the pinned `openai>=1.35.7` / installed
`2.48.0`. This fits the codebase's existing style — `Message`, `VectorStoreResult`,
`RagCitation` are already Pydantic `BaseModel`s — and removes a failure mode we'd
otherwise have to build ourselves: no regex/JSON extraction from free text, no
retry-on-malformed-output logic. Requires a structured-output-capable model (gpt-4o
family and later); both new calls should default to a cheaper/faster model than the main
conversational completion, since they're auxiliary, not user-facing content.

## 4. Pipeline stages

```
user message + chat history
        │
        ▼
[1] expand_query()            — app/query_expansion.py, new
        │  RetrievalQuery{query, topics}
        ▼
[2] vector_store.query()      — app/vector_store.py, existing, wider top_k
        │  list[VectorStoreResult]  (candidates)
        ▼
[3] rerank()                  — app/reranker.py, new
        │  list[VectorStoreResult] (trimmed to final k)
        ▼
[4] prepare_messages_with_sources() → get_chat_response_with_history()
        — app/rag_service.py + app/chat_gpt_client.py, existing, unchanged signatures
```

### 4.1 Stage 1 — Query expansion (`app/query_expansion.py`, new)

```python
class RetrievalQuery(BaseModel):
    query: str            # enriched retrieval query text
    topics: list[str]     # related Quaker-vocabulary terms considered

async def expand_query(
    user_message: str,
    chat_history: list[Message],
    model: str = QUERY_EXPANSION_MODEL,
) -> RetrievalQuery:
    ...
```

Own prompt file, `app/prompts/query_expansion_prompt.md`, loaded via the same
`_load_prompt` pattern `main.py` already uses for the system prompt. The prompt's job:
given the user's message (plus recent history for pronoun/context resolution), produce a
retrieval-oriented query enriched with Quaker-specific vocabulary the source texts
actually use (Inward Light, leadings, convincement, waiting upon the Lord, the Witness of
God, etc.) and a short list of related topic terms.

### 4.2 Stage 2 — Retrieval (existing, unchanged code)

`ChromaDBStore.query(expanded.query, top_k=RETRIEVAL_CANDIDATE_K)` — same method, just
called with the expanded query string and a wider candidate pool (proposed default 12,
see §6) than the final count passed to generation, so reranking has something to trim.

### 4.3 Stage 3 — Rerank (`app/reranker.py`, new)

```python
class RankedIndices(BaseModel):
    keep: list[int]   # indices into candidates, best-first, length <= keep_k

async def rerank(
    query: str,
    candidates: list[VectorStoreResult],
    keep_k: int = RETRIEVAL_FINAL_K,
    model: str = RERANK_MODEL,
) -> list[VectorStoreResult]:
    ...
```

Structured-output call, not a cross-encoder model. A cross-encoder is faster and avoids a
token-billed call, but it's a new model artifact to host — this project already paid real
deployment friction for exactly that with the local ONNX embedding model (`scripts/upload_spaces.py`,
the "download ONNX embedding model from Spaces at startup" fix). Starting LLM-only avoids
reopening that; swapping in a cross-encoder later is a self-contained change to this one
file if latency/cost data says it's worth it.

`RankedIndices` returns `keep: list[int]` — indices into the candidate list — specifically
so the model reorders/selects rather than regenerating passage text: cheaper (few output
tokens instead of full passages), faster, and structurally can't drift from the source
content the way regenerated text could. `rerank()` maps the returned indices back onto the
original `candidates` list; any out-of-range index from the model is dropped rather than
raising, since a single malformed index shouldn't fail the whole turn.

### 4.4 Stage 4 — Generation (existing, unchanged)

`RAGService.prepare_messages_with_sources()` keeps its current public signature — callers
in `app/main.py` don't change. Internally, `get_relevant_context()` becomes
expand → query(wider k) → rerank, instead of a single `vector_store.query()` call.

## 5. Orchestration point

```python
# app/rag_service.py
class RAGService:
    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store

    async def get_relevant_context(
        self, query: str, chat_history: list[Message], top_k: int = RETRIEVAL_FINAL_K
    ) -> Tuple[str, List[RagCitation]]:
        expanded = await expand_query(query, chat_history)
        candidates = await self.vector_store.query(expanded.query, RETRIEVAL_CANDIDATE_K)
        results = await rerank(expanded.query, candidates, keep_k=top_k)
        # ...existing context/citation-building logic, unchanged
```

`get_relevant_context` gains a `chat_history` parameter it doesn't have today (needed by
`expand_query` for pronoun/context resolution — "what did **he** say about that" needs the
prior turn). `prepare_messages_with_sources`, which already receives `chat_history`, just
passes it through. This is the only signature change, and it's internal to `RAGService` —
`main.py`'s call to `prepare_messages_with_sources` is unaffected.

`expanded.topics` is surfaced to the conversational model — appended to the system message
alongside the retrieved passages (e.g. "Related topics considered: Inward Light, leadings,
convincement") so the model has the vocabulary bridge even where no single passage matched
well. Surfacing topics to the *user* (e.g. as small chips near the response, hinting at the
vocabulary the search expanded into) is a plausible future UX addition — out of scope for
this pass, which only wires topics into the model-facing context; a future pass can revisit
once there's a citation-panel-like surface to hang it on.

## 6. Configuration

| Env var | Purpose | Proposed default |
|---|---|---|
| `QUERY_EXPANSION_MODEL` | Model for stage 1 | `gpt-4o-mini` |
| `RERANK_MODEL` | Model for stage 3 | `gpt-4o-mini` |
| `RETRIEVAL_CANDIDATE_K` | Stage 2 candidate pool size | `12` |
| `RETRIEVAL_FINAL_K` | Stage 3 output size (replaces today's hardcoded `top_k=5`) | `5` |

Same `os.getenv(..., default)` pattern already used throughout (`CHAT_GPT_MODEL`,
`CHAT_GPT_TEMPERATURE`, etc.) — no new config mechanism.

## 7. Testing approach

Mirrors the existing convention in `tests/test_main.py` — no real OpenAI calls in the
test suite:

- `expand_query` and `rerank` are each unit-testable in isolation with a monkeypatched
  `AsyncOpenAI` client (or a fake `.parse()` returning a fixed Pydantic instance),
  independent of retrieval or generation.
- `RAGService.get_relevant_context` gets its own tests with `expand_query`/`rerank`
  monkeypatched, verifying it wires candidates → reranked results → context string
  correctly.
- Existing `test_main.py` tests continue to monkeypatch
  `app.main.rag_service.prepare_messages_with_sources` directly and are unaffected — they
  never see the new internal stages.
- `MockVectorStore` (already in `app/vector_store.py`) still works for any test that wants
  a real `RAGService` without hitting Chroma; `expand_query`/`rerank` would need
  monkeypatching alongside it in that case, since they call OpenAI directly.

## 8. Rollout / validation plan

No runtime feature flag — this is a one-time decision, not a permanent branch point (see
`conversational_orientation.md`'s note against over-building for hypothetical futures).
Validate entirely through `scripts/prompt_playground.py` before merging:

1. Extend the playground script to print the expanded query + topics, the pre-rerank
   candidate list, and the post-rerank final list, alongside what it already shows.
2. Re-run the two test questions from §1 — confirm "discernment" now surfaces something
   better than editorial front matter, and confirm "that of God in everyone" doesn't
   regress.
3. Run a handful of repeated trials per question (generation is temperature-driven, as
   `conversational_orientation.md` §6 already documents) to check the fix holds up, not
   just a single lucky run.
4. Benchmark latency: print per-stage wall-clock time (expansion, retrieval, rerank,
   generation) alongside each playground turn. No hard budget enforced in code — this is
   basic visibility for judging whether the two new sequential calls are acceptable, not
   an optimization target (see §9.3).
5. Only then wire it into `app/main.py`'s existing `rag_service` instance and update
   `tests/test_main.py` mocks if the `get_relevant_context` signature change requires it.

**Validation results (playground, real pipeline, 2026-07-26):** the "how did George Fox
think about discernment" query — 0-of-3 quoting under the old single-stage retrieval (§1)
— now retrieves candidates scoring 0.65-0.69 (vs. mostly editorial/biographical noise
before) and reranks down to 4-5 passages that include a genuinely relevant William Penn
passage each time. Across 2 real trials, generation used verbatim fragments from that
passage both times ("never errs nor fails", "the gift of God in themselves") — a clear
improvement over the prior 0-of-3, though still partial-fragment quoting rather than a
full attributed block quote; that's a generation-prompt-side nuance, not a retrieval
problem, and is a candidate for a future prompt-tuning pass. The known-good "that of God
in everyone" query did not regress (0.6-0.66 scores, clean attributed block quote in the
reply). Rerank isn't perfectly precise yet — it still let a couple of biographical/editorial
passages through alongside the genuinely relevant one on the "discernment" query — but the
good passage consistently survives, which is the part that matters for generation.

Observed latency across these trials: expansion ~1.3-3.1s, retrieval ~0.25-0.4s, rerank
~0.85-1.1s, generation ~1.7-5.5s — roughly 4.4-8.4s total per turn, noticeably slower than
the single-call baseline. Per the user's guidance (§9.3), no hard budget is enforced; this
is recorded as a baseline to watch, not a blocker.

## 9. Decisions

Resolved by the user; recorded here so future edits don't relitigate them.

1. **Surface `topics` to the conversational model: yes.** Appended to the system message
   alongside retrieved passages (§5). Surfacing to the *user* is a plausible future UX
   addition, deliberately out of scope for this pass.
2. **`RETRIEVAL_CANDIDATE_K` / `RETRIEVAL_FINAL_K` defaults (12 / 5): accepted as starting
   guesses.** Not derived from this corpus — expect to tune after real use.
3. **Cost/latency tolerance: no hard budget.** Add basic per-stage timing to the playground
   (§8.4) so latency is visible, but don't over-optimize before real usage data exists.
4. **Rerank approach: LLM structured-output call, confirmed.** Explicitly index-based (see
   §4.3) rather than regenerating passage text, both for cost and to guarantee it can't
   drift from the source content — "reliably reproduces the results with minimal loss" is
   satisfied structurally, not just by prompting. Cross-encoder deferred unless latency/cost
   data later says otherwise.
