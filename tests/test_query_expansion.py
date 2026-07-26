import pytest
from unittest.mock import AsyncMock, patch

from app.chat_gpt_client import Message, MessageRole
from app.query_expansion import RetrievalQuery, expand_query


class MockParseResponse:
    def __init__(self, parsed):
        self.choices = [MockChoice(parsed)]


class MockChoice:
    def __init__(self, parsed):
        self.message = MockMessage(parsed)


class MockMessage:
    def __init__(self, parsed):
        self.parsed = parsed


@pytest.mark.asyncio
@patch("app.query_expansion.client")
async def test_expand_query_returns_parsed_result(mock_client):
    expected = RetrievalQuery(
        query="discernment Inward Light leadings", topics=["Inward Light", "leadings"]
    )
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(expected)
    )

    result = await expand_query("how did Fox think about discernment?", [])

    assert result == expected


@pytest.mark.asyncio
@patch("app.query_expansion.client")
async def test_expand_query_falls_back_to_original_message_on_error(mock_client):
    mock_client.chat.completions.parse = AsyncMock(side_effect=Exception("API error"))

    result = await expand_query("what is simplicity?", [])

    assert result.query == "what is simplicity?"
    assert result.topics == []


@pytest.mark.asyncio
@patch("app.query_expansion.client")
async def test_expand_query_falls_back_when_parse_returns_none(mock_client):
    mock_client.chat.completions.parse = AsyncMock(return_value=MockParseResponse(None))

    result = await expand_query("what is simplicity?", [])

    assert result.query == "what is simplicity?"
    assert result.topics == []


@pytest.mark.asyncio
@patch("app.query_expansion.client")
async def test_expand_query_includes_recent_history_for_context(mock_client):
    expected = RetrievalQuery(query="Fox's view of the Inward Light", topics=["Inward Light"])
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(expected)
    )
    chat_history = [
        Message(role=MessageRole.user, content="Tell me about Fox"),
        Message(role=MessageRole.assistant, content="He founded the Quakers."),
    ]

    await expand_query("what did he think about it?", chat_history)

    sent_messages = mock_client.chat.completions.parse.call_args.kwargs["messages"]
    user_content = sent_messages[-1]["content"]
    assert "Tell me about Fox" in user_content
    assert "what did he think about it?" in user_content
