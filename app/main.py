import asyncio
import contextlib
import os
import re
import time
from contextlib import asynccontextmanager

import markdown2
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import tts_client
from app.chat_gpt_client import (
    CHAT_GPT_DEFAULT_MAX_TOKENS,
    Message,
    MessageRole,
    get_chat_response_with_history,
)
from app.rag_service import RAGService
from app.session import (
    CHAT_RATE_LIMIT,
    SESSION_SWEEP_INTERVAL_SECONDS,
    TTS_RATE_LIMIT,
    SessionState,
    get_session,
    rate_limit_key,
    session_store,
    set_session_cookie,
)
from app.tts_service import TTSUpstreamError, TTSUpstreamTimeoutError, tts_service
from app.vector_store import ChromaDBStore

TTS_MAX_INPUT_CHARS = int(os.getenv("TTS_MAX_INPUT_CHARS", "3800"))
CHAT_GPT_MAX_TOKENS_AUDIO = int(os.getenv("CHAT_GPT_MAX_TOKENS_AUDIO", "800"))

TTS_ERROR_MESSAGE_NOT_FOUND = "message not found"
TTS_ERROR_MESSAGE_TOO_LONG = "message too long for speech synthesis"
TTS_ERROR_GENERATION_FAILED = "speech generation failed"
TTS_ERROR_GENERATION_TIMED_OUT = "speech generation timed out"
TTS_ERROR_MISCONFIGURED = "speech generation is misconfigured"  # never returned in
# normal operation — startup config validation (validate_config, below) rejects a
# misconfigured deployment before it ever serves a request.
RATE_LIMIT_ERROR_MESSAGE = "rate limit exceeded"

SYSTEM_PROMPT = "<system-prompt>You are a humble, steady companion grounded in the writings of George Fox and the history, faith, and practice of the Religious Society of Friends (Quakers). Speak plainly and honestly, sharing what you know with care. You're here for more than questions about Quakerism itself: help people think through everyday life—politics, family, work, relationships, hard choices, and other daily concerns—through a Quakerly lens. Every answer, on any topic, should read as distinctly Quaker in perspective—drawing on convictions like the Inward Light, plain speech, simplicity, integrity, equality, and discernment—rather than offering generic advice that happens to mention Friends. Draw on the historic Quaker texts available to you as grounding: for every response, actively look through the retrieved passages for at least one that is genuinely relevant to the question, and when you find one, quote it directly, verbatim from the retrieved text, and attribute it using only the author and source given in that passage's metadata—never invent, paraphrase-as-quote, or misattribute a quotation. Make a real effort to ground each answer in such a quotation rather than reaching for one only when it happens to be obvious. If no retrieved passage is genuinely relevant, do not fabricate one: paraphrase your own understanding instead and say plainly that no direct passage was found for this question. Keep replies concise and proportional to the question—a brief question deserves a brief answer; reserve length for questions that truly call for depth, and never pad a response just to seem thorough. Decline messages that are harmful or cruel, treating everyone with dignity while staying true to your purpose. Remember: there is something sacred in every person; hold space for that truth even when turning them away.</system-prompt>"

AUDIO_MODE_ADDENDUM = (
    "The user has audio playback enabled and will likely listen to this response rather "
    "than read it. Favor a succinct answer, mindful that it will be listened to rather "
    "than skimmed — but don't sacrifice completeness or clarity for brevity's sake. A few "
    "well-formed paragraphs are better than one terse one if the question genuinely calls "
    "for it. Keep any quotation brief enough to fit naturally within the response."
)

_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]\s")


def _trim_to_sentence_boundary(text: str) -> str:
    """Trim truncated (finish_reason == "length") text to its last full sentence.

    Falls back to the raw truncated text if no sentence boundary is found at all
    — a truncated fragment is still less misleading than nothing, and this is a
    backstop, not the primary brevity mechanism (see AUDIO_MODE_ADDENDUM).
    """
    last_boundary = None
    for match in _SENTENCE_BOUNDARY_RE.finditer(text):
        last_boundary = match
    if last_boundary is None:
        return text
    return text[: last_boundary.end()].rstrip()


def validate_config() -> None:
    """Fail fast on invalid TTS configuration, rather than surfacing it lazily on
    the first request (session_isolation.md's posture for SESSION_SECRET_KEY,
    extended here to TTS's env-backed settings — text_to_speech.md §6)."""
    if not (0.25 <= tts_client.TTS_SPEED <= 4.0):
        raise RuntimeError(
            f"TTS_SPEED must be between 0.25 and 4.0, got {tts_client.TTS_SPEED}"
        )
    if not tts_client.TTS_VOICE.strip():
        raise RuntimeError("TTS_VOICE must not be empty")
    if not tts_client.TTS_MODEL.strip():
        raise RuntimeError("TTS_MODEL must not be empty")


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(SESSION_SWEEP_INTERVAL_SECONDS)
        session_store.sweep_expired(time.time())


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    sweep_task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task


limiter = Limiter(key_func=rate_limit_key)

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    response = JSONResponse({"detail": RATE_LIMIT_ERROR_MESSAGE}, status_code=429)
    return limiter._inject_headers(response, request.state.view_rate_limit)


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


@app.middleware("http")
async def session_cookie_middleware(request: Request, call_next):
    """Sets/refreshes the session cookie on whatever Response the route produced.

    Done here, not via an injected `Response` dependency, because FastAPI doesn't
    merge an injected Response's headers into a route's own returned Response
    subclass (TemplateResponse, etc.) — only middleware sees the actual final
    Response object regardless of its type. get_session() stashes the resolved
    session id on request.state for this middleware to pick up.
    """
    response = await call_next(request)
    session_id = getattr(request.state, "session_id", None)
    if session_id is not None:
        set_session_cookie(response, session_id)
    return response


templates_directory = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_directory)

# Mount the static directory
static_directory = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_directory), name="static")

# Get the absolute path to the project root
project_root = os.path.dirname(os.path.abspath(__file__))

# Initialize RAG service with ChromaDBStore
chroma_db_path = os.path.join(project_root, "db")
vector_store = ChromaDBStore(path=chroma_db_path, collection_name="quaker_texts")
rag_service = RAGService(vector_store)


@app.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request, session: SessionState = Depends(get_session)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "chat_history": session.chat_history,
        },
    )


@app.post("/chat")
@limiter.limit(CHAT_RATE_LIMIT)
async def chat(
    request: Request,
    message: str = Form(...),
    audio_mode: bool = Form(False),
    session: SessionState = Depends(get_session),
) -> HTMLResponse:
    system_instructions = [SYSTEM_PROMPT]
    if audio_mode:
        system_instructions.append(AUDIO_MODE_ADDENDUM)
    # joined at the call site, not implicitly, so this stays legible if
    # prepare_messages_with_sources's system-message handling ever changes
    system_prompt = " ".join(system_instructions)

    # Prepare messages with the correct order
    prepared_messages, citations = await rag_service.prepare_messages_with_sources(
        system_prompt=system_prompt,
        chat_history=session.chat_history[-5:],  # Last 5 messages for context
        user_message=message,
    )

    max_tokens = CHAT_GPT_MAX_TOKENS_AUDIO if audio_mode else CHAT_GPT_DEFAULT_MAX_TOKENS
    completion = await get_chat_response_with_history(
        prepared_messages, max_tokens=max_tokens
    )
    bot_response = completion.content
    response_truncated = completion.finish_reason == "length"
    if response_truncated:
        bot_response = _trim_to_sentence_boundary(bot_response)

    # Render Markdown to HTML (with safety features)
    bot_response_html = markdown2.markdown(bot_response, safe_mode="escape")

    tts_available = len(bot_response) <= TTS_MAX_INPUT_CHARS

    # Add user message and bot response to the caller's session only
    now = time.time()
    session_store.append_message(
        session.session_id, Message(role=MessageRole.user, content=message), now
    )
    bot_message = Message(
        role=MessageRole.assistant,
        content=bot_response,
        tts_available=tts_available,
    )
    session_store.append_message(session.session_id, bot_message, now)

    response_html = templates.TemplateResponse(
        request,
        "bot_message.html",
        {
            "bot_response_html": bot_response_html,
            "citations": citations,
            "message_id": bot_message.id,
            "tts_available": tts_available,
            "response_truncated": response_truncated,
        },
    )

    return response_html


@app.get("/api/chat_history")
async def get_chat_history(
    session: SessionState = Depends(get_session),
) -> list[dict]:
    return [message.model_dump() for message in session.chat_history]


# Optional: Add a route to clear chat history (for testing/demo purposes)
@app.post("/api/clear_history")
async def clear_history(session: SessionState = Depends(get_session)) -> dict[str, str]:
    session_store.clear(session.session_id, time.time())
    return {"message": "Chat history cleared"}


@app.post("/api/messages/{message_id}/speech")
@limiter.limit(TTS_RATE_LIMIT)
async def synthesize_message_speech(
    request: Request,
    message_id: str,
    session: SessionState = Depends(get_session),
) -> Response:
    message = session.messages_by_id.get(message_id)
    if message is None:
        raise HTTPException(404, TTS_ERROR_MESSAGE_NOT_FOUND)
    if not message.tts_available:
        raise HTTPException(413, TTS_ERROR_MESSAGE_TOO_LONG)
    try:
        audio = await tts_service.get_or_generate(
            session.session_id, message_id, message.content
        )
    except TTSUpstreamTimeoutError:
        raise HTTPException(504, TTS_ERROR_GENERATION_TIMED_OUT)
    except TTSUpstreamError:
        raise HTTPException(502, TTS_ERROR_GENERATION_FAILED)
    return Response(content=audio, media_type="audio/mpeg")
