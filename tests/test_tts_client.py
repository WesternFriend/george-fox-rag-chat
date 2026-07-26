import pytest
from unittest.mock import MagicMock, patch

from app.tts_client import synthesize_speech


class MockStreamingResponse:
    """Stands in for openai's AsyncResponseContextManager / binary response."""

    def __init__(self, content: bytes):
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def read(self) -> bytes:
        return self._content


@pytest.mark.asyncio
@patch("app.tts_client.client")
async def test_synthesize_speech_returns_bytes_on_success(mock_client):
    mock_create = MagicMock(return_value=MockStreamingResponse(b"mock-mp3-bytes"))
    mock_client.audio.speech.with_streaming_response.create = mock_create

    audio = await synthesize_speech("Hello there")

    assert audio == b"mock-mp3-bytes"
    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["input"] == "Hello there"


@pytest.mark.asyncio
@patch("app.tts_client.client")
async def test_synthesize_speech_raises_on_api_error(mock_client):
    def _raise(*args, **kwargs):
        raise RuntimeError("upstream failure")

    mock_client.audio.speech.with_streaming_response.create = MagicMock(side_effect=_raise)

    with pytest.raises(RuntimeError):
        await synthesize_speech("Hello there")
