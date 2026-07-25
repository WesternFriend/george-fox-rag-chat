import pytest

from app.chat_gpt_client import Message, MessageRole
from app.rag_service import RAGService
from app.vector_store import VectorStore, VectorStoreMetadata, VectorStoreResult


class StubVectorStore(VectorStore):
    def __init__(self, results):
        self.results = results
        self.last_query = None
        self.last_top_k = None

    async def query(self, query: str, top_k: int = 5):
        self.last_query = query
        self.last_top_k = top_k
        return self.results


@pytest.fixture
def two_results():
    return [
        VectorStoreResult(
            content="That which is spiritual must be spiritually discerned.",
            metadata=VectorStoreMetadata(
                score=0.9,
                source="fox_journal.txt",
                title="The Journal of George Fox",
                authors="George Fox",
            ),
        ),
        VectorStoreResult(
            content="Let your lives preach.",
            metadata=VectorStoreMetadata(
                score=0.8,
                source="no_title_no_author.txt",
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_get_relevant_context_includes_title_and_author(two_results):
    service = RAGService(StubVectorStore(two_results))

    context, citations = await service.get_relevant_context("what is discernment?")

    assert "The Journal of George Fox" in context
    assert "George Fox" in context
    assert "That which is spiritual must be spiritually discerned." in context
    assert len(citations) == 2
    assert citations[0].title == "The Journal of George Fox"
    assert citations[0].authors == "George Fox"


@pytest.mark.asyncio
async def test_get_relevant_context_falls_back_to_source_without_title_or_author(
    two_results,
):
    service = RAGService(StubVectorStore(two_results))

    context, _ = await service.get_relevant_context("let your lives preach")

    # Second result has neither title nor authors, so it should fall back to source
    assert "no_title_no_author.txt" in context
    assert "Let your lives preach." in context


@pytest.mark.asyncio
async def test_prepare_messages_with_sources_grounds_system_prompt(two_results):
    service = RAGService(StubVectorStore(two_results))

    messages, citations = await service.prepare_messages_with_sources(
        system_prompt="Be a Quakerly companion.",
        chat_history=[Message(role=MessageRole.user, content="Hi")],
        user_message="What did Fox say about discernment?",
    )

    system_message = messages[0]
    assert system_message.role == MessageRole.system
    assert "Be a Quakerly companion." in system_message.content
    assert "George Fox" in system_message.content
    assert messages[-1].content == "What did Fox say about discernment?"
    assert len(citations) == 2
