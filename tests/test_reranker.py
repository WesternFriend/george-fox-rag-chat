import pytest
from unittest.mock import AsyncMock, patch

from app.reranker import RankedIndices, rerank
from app.vector_store import VectorStoreMetadata, VectorStoreResult


class MockParseResponse:
    def __init__(self, parsed):
        self.choices = [MockChoice(parsed)]


class MockChoice:
    def __init__(self, parsed):
        self.message = MockMessage(parsed)


class MockMessage:
    def __init__(self, parsed):
        self.parsed = parsed


@pytest.fixture
def three_candidates():
    return [
        VectorStoreResult(
            content="Editorial preface about the edition.",
            metadata=VectorStoreMetadata(score=0.9, source="preface.txt"),
        ),
        VectorStoreResult(
            content="That which is spiritual must be spiritually discerned.",
            metadata=VectorStoreMetadata(score=0.85, source="fox_journal.txt"),
        ),
        VectorStoreResult(
            content="Fox was born in Drayton-in-the-Clay in 1624.",
            metadata=VectorStoreMetadata(score=0.8, source="biography.txt"),
        ),
    ]


@pytest.mark.asyncio
async def test_rerank_returns_empty_list_for_no_candidates():
    result = await rerank("discernment", [], keep_k=5)
    assert result == []


@pytest.mark.asyncio
@patch("app.reranker.client")
async def test_rerank_maps_indices_back_to_candidates(mock_client, three_candidates):
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(RankedIndices(keep=[1]))
    )

    result = await rerank("discernment", three_candidates, keep_k=5)

    assert len(result) == 1
    assert result[0] is three_candidates[1]


@pytest.mark.asyncio
@patch("app.reranker.client")
async def test_rerank_preserves_model_ordering(mock_client, three_candidates):
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(RankedIndices(keep=[2, 0]))
    )

    result = await rerank("Fox", three_candidates, keep_k=5)

    assert result == [three_candidates[2], three_candidates[0]]


@pytest.mark.asyncio
@patch("app.reranker.client")
async def test_rerank_truncates_to_keep_k(mock_client, three_candidates):
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(RankedIndices(keep=[0, 1, 2]))
    )

    result = await rerank("discernment", three_candidates, keep_k=2)

    assert len(result) == 2


@pytest.mark.asyncio
@patch("app.reranker.client")
async def test_rerank_drops_out_of_range_indices(mock_client, three_candidates):
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(RankedIndices(keep=[1, 99, -1]))
    )

    result = await rerank("discernment", three_candidates, keep_k=5)

    assert result == [three_candidates[1]]


@pytest.mark.asyncio
@patch("app.reranker.client")
async def test_rerank_allows_legitimately_empty_result(mock_client, three_candidates):
    """The model may correctly decide nothing is relevant enough to keep —
    that's not a failure, and shouldn't fall back to raw candidates."""
    mock_client.chat.completions.parse = AsyncMock(
        return_value=MockParseResponse(RankedIndices(keep=[]))
    )

    result = await rerank("an unrelated topic", three_candidates, keep_k=5)

    assert result == []


@pytest.mark.asyncio
@patch("app.reranker.client")
async def test_rerank_falls_back_to_raw_candidates_on_error(mock_client, three_candidates):
    mock_client.chat.completions.parse = AsyncMock(side_effect=Exception("API error"))

    result = await rerank("discernment", three_candidates, keep_k=2)

    assert result == three_candidates[:2]
