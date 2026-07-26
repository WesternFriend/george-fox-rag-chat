"""CLI for iterating on app/prompts/*.md without going through the web UI.

Exercises the real pipeline — ChromaDBStore against app/db, RAGService (query
expansion -> retrieval -> rerank -> generation), and live OpenAI calls — so
responses reflect actual retrieval and generation behavior. Skips the
FastAPI/session/HTML-rendering layers, since those aren't what prompt tuning
needs to verify.

RETRIEVAL_CANDIDATE_K / RETRIEVAL_FINAL_K / QUERY_EXPANSION_MODEL / RERANK_MODEL
env vars (see .env) control the retrieval pipeline the same way they do for the
live app — set them before running to experiment with different sizes/models.

Usage:
    uv run python scripts/prompt_playground.py "How did Fox think about discernment?"
    uv run python scripts/prompt_playground.py --repl
    uv run python scripts/prompt_playground.py --repl --audio --show-context
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Run directly as a script (not `-m`), so the project root isn't on sys.path
# by default — add it so `app.*` resolves the same as it does under uvicorn.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.rag_service as rag_service_module  # noqa: E402
from app.chat_gpt_client import (  # noqa: E402
    CHAT_GPT_DEFAULT_MAX_TOKENS,
    CHAT_GPT_DEFAULT_MODEL,
    CHAT_GPT_DEFAULT_TEMPERATURE,
    Message,
    MessageRole,
    get_chat_response_with_history,
)
from app.rag_service import RAGService  # noqa: E402
from app.vector_store import ChromaDBStore  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "app" / "prompts"
CHROMA_DB_PATH = PROJECT_ROOT / "app" / "db"


def _load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


def build_system_prompt(audio: bool) -> str:
    system_prompt = f"<system-prompt>{_load_prompt('system_prompt.md')}</system-prompt>"
    if audio:
        system_prompt = f"{system_prompt}\n\n{_load_prompt('audio_mode_addendum.md')}"
    return system_prompt


def _label(citation) -> str:
    label = citation.title or citation.source
    if citation.authors:
        label = f"{label} ({citation.authors})"
    return label


def print_turn(stage_data, timings, result, show_context: bool) -> None:
    expanded = stage_data.get("expanded")
    if expanded is not None:
        print("--- query expansion ---")
        print(f"  query:  {expanded.query}")
        print(f"  topics: {', '.join(expanded.topics) or '(none)'}")

    candidates = stage_data.get("candidates")
    if candidates is not None:
        print(f"\n--- candidates (pre-rerank, {len(candidates)}) ---")
        for c in candidates:
            label = c.metadata.title or c.metadata.source
            print(f"  [{label}] score={c.metadata.score:.3f}")

    reranked = stage_data.get("citations")
    if reranked:
        print(f"\n--- kept after rerank ({len(reranked)}) ---")
        for c in reranked:
            print(f"  [{_label(c)}]")
            if show_context:
                print(f"    {c.content}\n")
    else:
        print("\n--- kept after rerank: none ---")

    print(
        "\n--- timings (s) --- "
        f"expand={timings.get('expansion', 0):.2f} "
        f"retrieve={timings.get('retrieval', 0):.2f} "
        f"rerank={timings.get('rerank', 0):.2f} "
        f"generate={timings.get('generation', 0):.2f} "
        f"total={sum(timings.values()):.2f}"
    )

    print(f"\n--- response (finish_reason={result.finish_reason}) ---")
    print(result.content)


async def run_turn(
    rag_service: RAGService,
    system_prompt: str,
    chat_history: list[Message],
    user_message: str,
    model: str,
    temperature: float,
    max_tokens: int,
    show_context: bool,
) -> Message:
    stage_data: dict = {}
    timings: dict = {}

    real_expand_query = rag_service_module.expand_query
    real_rerank = rag_service_module.rerank
    real_query = rag_service.vector_store.query

    async def timed_expand_query(user_message, chat_history, model=None):
        t0 = time.perf_counter()
        result = await real_expand_query(user_message, chat_history)
        timings["expansion"] = time.perf_counter() - t0
        stage_data["expanded"] = result
        return result

    async def timed_query(query, top_k=5):
        t0 = time.perf_counter()
        result = await real_query(query, top_k)
        timings["retrieval"] = time.perf_counter() - t0
        stage_data["candidates"] = result
        return result

    async def timed_rerank(query, candidates, keep_k, model=None):
        t0 = time.perf_counter()
        result = await real_rerank(query, candidates, keep_k=keep_k)
        timings["rerank"] = time.perf_counter() - t0
        return result

    rag_service_module.expand_query = timed_expand_query
    rag_service_module.rerank = timed_rerank
    rag_service.vector_store.query = timed_query
    try:
        messages, citations = await rag_service.prepare_messages_with_sources(
            system_prompt, chat_history, user_message
        )
    finally:
        rag_service_module.expand_query = real_expand_query
        rag_service_module.rerank = real_rerank
        rag_service.vector_store.query = real_query
    stage_data["citations"] = citations

    t0 = time.perf_counter()
    result = await get_chat_response_with_history(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    timings["generation"] = time.perf_counter() - t0

    print_turn(stage_data, timings, result, show_context)
    return Message(role=MessageRole.assistant, content=result.content)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="?", help="A single message to send (omit for --repl)")
    parser.add_argument("--repl", action="store_true", help="Interactive multi-turn session")
    parser.add_argument("--audio", action="store_true", help="Append the audio-mode addendum")
    parser.add_argument(
        "--show-context", action="store_true", help="Print full retrieved passage text"
    )
    parser.add_argument("--model", default=CHAT_GPT_DEFAULT_MODEL)
    parser.add_argument("--temperature", type=float, default=CHAT_GPT_DEFAULT_TEMPERATURE)
    parser.add_argument("--max-tokens", type=int, default=CHAT_GPT_DEFAULT_MAX_TOKENS)
    args = parser.parse_args()

    if not args.repl and not args.message:
        parser.error("provide a message, or pass --repl for an interactive session")

    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY not set (check .env)")

    system_prompt = build_system_prompt(args.audio)
    vector_store = ChromaDBStore(path=str(CHROMA_DB_PATH), collection_name="quaker_texts")
    rag_service = RAGService(vector_store)

    chat_history: list[Message] = []

    if not args.repl:
        await run_turn(
            rag_service,
            system_prompt,
            chat_history,
            args.message,
            args.model,
            args.temperature,
            args.max_tokens,
            args.show_context,
        )
        return

    print("Prompt playground — multi-turn REPL. Type 'exit' or Ctrl-D to quit.\n")
    while True:
        try:
            user_message = input("you> ").strip()
        except EOFError:
            print()
            break
        if not user_message:
            continue
        if user_message.lower() in {"exit", "quit"}:
            break

        chat_history.append(Message(role=MessageRole.user, content=user_message))
        assistant_message = await run_turn(
            rag_service,
            system_prompt,
            chat_history[:-1],
            user_message,
            args.model,
            args.temperature,
            args.max_tokens,
            args.show_context,
        )
        chat_history.append(assistant_message)
        print()


if __name__ == "__main__":
    asyncio.run(main())
