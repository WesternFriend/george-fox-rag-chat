import pytest
from unittest.mock import AsyncMock, patch
from app.chat_gpt_client import get_chat_response_with_history, Message, MessageRole


# Fixture for chat history
@pytest.fixture
def chat_history():
    return [
        Message(role=MessageRole.user, content="Hello"),
        Message(role=MessageRole.assistant, content="Hi! How can I help you today?"),
    ]


# Fixture for environment variables
@pytest.fixture
def load_env_variables(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test_api_key")


@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_success(
    mock_client, chat_history, load_env_variables
):
    mock_client.chat.completions.create = AsyncMock(return_value=MockResponse())
    result = await get_chat_response_with_history(chat_history)
    assert result.content == "Mocked response content"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_api_error(
    mock_client, chat_history, load_env_variables
):
    mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API error"))
    result = await get_chat_response_with_history(chat_history)
    assert "I'm sorry, but I encountered an error: API error" in result.content
    assert result.finish_reason == "error"


@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_empty_message(
    mock_client, load_env_variables
):
    # Using AsyncMock to simulate async API call
    mock_client.chat.completions.create = AsyncMock(return_value=MockResponse())
    result = await get_chat_response_with_history([])
    # Adjusted assertion to match expected response for an empty history scenario
    assert (
        result.content == "Mocked response content"
    )  # Assuming the API handles empty histories gracefully


@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_surfaces_length_finish_reason(
    mock_client, chat_history, load_env_variables
):
    mock_client.chat.completions.create = AsyncMock(
        return_value=MockResponse(finish_reason="length")
    )
    result = await get_chat_response_with_history(chat_history)
    assert result.finish_reason == "length"


@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_sends_plain_role_content_dicts(
    mock_client, chat_history, load_env_variables
):
    """Message now carries id/tts_available fields that must never leak into the
    OpenAI request payload — only role/content belong there."""
    mock_client.chat.completions.create = AsyncMock(return_value=MockResponse())
    await get_chat_response_with_history(chat_history)

    sent_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    for sent, original in zip(sent_messages, chat_history):
        assert sent == {"role": original.role.value, "content": original.content}


@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_never_injects_its_own_system_message(
    mock_client, load_env_variables
):
    """Regression test: this function must send `messages` as-is, with no extra
    system message prepended — RAGService already builds the real (grounded,
    citation-instructing) system message as messages[0]. A silently-injected
    generic system message ahead of it would dilute those instructions."""
    mock_client.chat.completions.create = AsyncMock(return_value=MockResponse())
    grounded_system_message = Message(
        role=MessageRole.system, content="<system-prompt>Quote your sources.</system-prompt>"
    )
    messages = [grounded_system_message, Message(role=MessageRole.user, content="Hi")]

    await get_chat_response_with_history(messages)

    sent_messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert len(sent_messages) == 2
    assert sent_messages[0] == {"role": "system", "content": grounded_system_message.content}


@pytest.mark.parametrize(
    "message_content,expected",
    [
        (["Hello"], "Mocked response content"),
        ([], "Mocked response content"),  # Testing with an empty history
    ],
)
@pytest.mark.asyncio
@patch("app.chat_gpt_client.client")
async def test_get_chat_response_with_history_parameterized(
    mock_client, message_content, expected, load_env_variables
):
    mock_client.chat.completions.create = AsyncMock(return_value=MockResponse())
    messages = [
        Message(role=MessageRole.user, content=content) for content in message_content
    ]
    result = await get_chat_response_with_history(messages)
    assert result.content == expected


class MockResponse:
    def __init__(self, finish_reason="stop"):
        self.choices = [MockChoice(finish_reason)]


class MockChoice:
    def __init__(self, finish_reason="stop"):
        self.message = MockMessage()
        self.finish_reason = finish_reason


class MockMessage:
    def __init__(self):
        self.content = "Mocked response content"
