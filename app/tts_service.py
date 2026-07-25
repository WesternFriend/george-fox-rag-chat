"""Caching, locking, and concurrency control around speech synthesis.

See docs/specifications/text_to_speech.md §4.4. Does no text preparation or
length checking itself — that's decided at /chat time (§4.2) — so it only ever
receives already-validated text and is purely about caching/locking/calling the
API.
"""

import asyncio
import os
from collections import OrderedDict, defaultdict
from dataclasses import dataclass

from app.tts_client import synthesize_speech

TTS_AUDIO_CACHE_MAX_ENTRIES = int(os.getenv("TTS_AUDIO_CACHE_MAX_ENTRIES", "200"))
TTS_AUDIO_CACHE_MAX_BYTES = int(
    os.getenv("TTS_AUDIO_CACHE_MAX_BYTES", str(100 * 1024 * 1024))
)  # 100 MB
TTS_MAX_CONCURRENT_REQUESTS = int(os.getenv("TTS_MAX_CONCURRENT_REQUESTS", "4"))


@dataclass
class TTSConfig:
    max_entries: int = TTS_AUDIO_CACHE_MAX_ENTRIES
    max_bytes: int = TTS_AUDIO_CACHE_MAX_BYTES
    max_concurrent_requests: int = TTS_MAX_CONCURRENT_REQUESTS


class TTSUpstreamError(Exception):
    pass


class TTSUpstreamTimeoutError(Exception):
    pass


class TTSService:
    def __init__(self, config: TTSConfig):
        self._config = config
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_bytes = 0
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)

    async def get_or_generate(self, session_id: str, message_id: str, text: str) -> bytes:
        key = (session_id, message_id)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        async with self._locks[key]:
            if key in self._cache:  # another request may have just finished
                self._cache.move_to_end(key)
                return self._cache[key]

            async with self._semaphore:  # bounds concurrent upstream calls, independent of the cache
                try:
                    audio = await synthesize_speech(text)
                except asyncio.TimeoutError:
                    raise TTSUpstreamTimeoutError()
                except Exception as e:
                    raise TTSUpstreamError() from e

            self._cache_put(key, audio)
            return audio

    def _cache_put(self, key: tuple[str, str], audio: bytes) -> None:
        self._cache[key] = audio
        self._cache.move_to_end(key)
        self._cache_bytes += len(audio)
        while self._cache and (
            len(self._cache) > self._config.max_entries
            or self._cache_bytes > self._config.max_bytes
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted)


tts_service = TTSService(TTSConfig())
