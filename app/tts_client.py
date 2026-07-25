"""Thin wrapper around OpenAI's speech-synthesis API.

Mirrors app/chat_gpt_client.py's shape. Deliberately thin — no caching, no
ownership checks, no text preparation here; those live in app/tts_service.py.
See docs/specifications/text_to_speech.md §4.1.
"""

import logging
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "cedar")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
TTS_TIMEOUT_SECONDS = float(os.getenv("TTS_TIMEOUT_SECONDS", "30"))
AUDIO_RESPONSE_FORMAT = "mp3"  # fixed, not configurable — see spec §2, §4.3

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


async def synthesize_speech(text: str) -> bytes:
    """Call OpenAI's speech endpoint and return raw MP3 bytes, or raise.

    Uses the SDK's `with_streaming_response` binary-response pattern (the
    documented usage for `client.audio.speech.create`, confirmed against the
    pinned `openai` package version) rather than a bare `.create()` call — still
    fully buffers server-side before returning, since this app doesn't stream
    audio through to the browser.

    Doesn't swallow exceptions, unlike get_chat_response_with_history — the
    caller (TTSService) needs to distinguish failure modes to map them to the
    right HTTP status.
    """
    async with client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        speed=TTS_SPEED,
        response_format=AUDIO_RESPONSE_FORMAT,
        timeout=TTS_TIMEOUT_SECONDS,
    ) as response:
        return await response.read()
