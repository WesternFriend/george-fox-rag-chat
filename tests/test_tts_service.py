import asyncio

import pytest

from app.tts_service import TTSConfig, TTSService, TTSUpstreamError


@pytest.mark.asyncio
async def test_cache_hit_skips_synthesis(monkeypatch):
    calls = []

    async def fake_synthesize(text):
        calls.append(text)
        return b"audio-bytes"

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize)
    service = TTSService(TTSConfig())

    first = await service.get_or_generate("session-1", "msg-1", "hello")
    second = await service.get_or_generate("session-1", "msg-1", "hello")

    assert first == b"audio-bytes"
    assert second == b"audio-bytes"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_same_message_id_different_sessions_are_distinct_cache_entries(monkeypatch):
    calls = []

    async def fake_synthesize(text):
        calls.append(text)
        return f"audio-for-{text}".encode()

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize)
    service = TTSService(TTSConfig())

    a = await service.get_or_generate("session-a", "same-message-id", "text-a")
    b = await service.get_or_generate("session-b", "same-message-id", "text-b")

    assert a != b
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_concurrent_requests_for_same_key_synthesize_once(monkeypatch):
    calls = []

    async def fake_synthesize(text):
        calls.append(text)
        await asyncio.sleep(0.05)
        return b"audio-bytes"

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize)
    service = TTSService(TTSConfig())

    results = await asyncio.gather(
        service.get_or_generate("session-1", "msg-1", "hello"),
        service.get_or_generate("session-1", "msg-1", "hello"),
    )

    assert results == [b"audio-bytes", b"audio-bytes"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_synthesis_leaves_no_cache_entry_and_retries(monkeypatch):
    attempts = {"count": 0}

    async def flaky_synthesize(text):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("upstream failure")
        return b"audio-bytes"

    monkeypatch.setattr("app.tts_service.synthesize_speech", flaky_synthesize)
    service = TTSService(TTSConfig())

    with pytest.raises(TTSUpstreamError):
        await service.get_or_generate("session-1", "msg-1", "hello")

    # not poisoned — a retry re-attempts and succeeds
    result = await service.get_or_generate("session-1", "msg-1", "hello")
    assert result == b"audio-bytes"
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_eviction_by_entry_count(monkeypatch):
    async def fake_synthesize(text):
        return text.encode()

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize)
    service = TTSService(TTSConfig(max_entries=2, max_bytes=10_000))

    await service.get_or_generate("s", "msg-1", "one")
    await service.get_or_generate("s", "msg-2", "two")
    await service.get_or_generate("s", "msg-3", "three")

    assert ("s", "msg-1") not in service._cache
    assert ("s", "msg-2") in service._cache
    assert ("s", "msg-3") in service._cache


@pytest.mark.asyncio
async def test_eviction_by_byte_budget(monkeypatch):
    async def fake_synthesize(text):
        return b"x" * 10  # 10 bytes each

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize)
    service = TTSService(TTSConfig(max_entries=100, max_bytes=25))

    await service.get_or_generate("s", "msg-1", "one")
    await service.get_or_generate("s", "msg-2", "two")
    await service.get_or_generate("s", "msg-3", "three")

    # 30 bytes total would exceed the 25-byte budget — oldest evicted first
    assert ("s", "msg-1") not in service._cache
    assert ("s", "msg-2") in service._cache
    assert ("s", "msg-3") in service._cache
