import pytest

from app.chat_gpt_client import Message, MessageRole
from app.query_expansion import RetrievalQuery
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


@pytest.fixture(autouse=True)
def stub_expand_and_rerank(monkeypatch):
    """expand_query and rerank both call OpenAI directly — stub them to identity
    behavior (pass the query through unchanged, keep every candidate up to
    keep_k) so these tests exercise RAGService's own wiring, not the network.
    Individual tests override these via monkeypatch when they need to assert
    on the wiring itself.
    """

    async def fake_expand_query(user_message, chat_history, model=None):
        return RetrievalQuery(query=user_message, topics=[])

    async def fake_rerank(query, candidates, keep_k, model=None):
        return candidates[:keep_k]

    monkeypatch.setattr("app.rag_service.expand_query", fake_expand_query)
    monkeypatch.setattr("app.rag_service.rerank", fake_rerank)


@pytest.mark.asyncio
async def test_get_relevant_context_includes_title_and_author(two_results):
    service = RAGService(StubVectorStore(two_results))

    context, citations, _ = await service.get_relevant_context("what is discernment?")

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

    context, _, _ = await service.get_relevant_context("let your lives preach")

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


@pytest.mark.asyncio
async def test_get_relevant_context_queries_with_expanded_query(two_results, monkeypatch):
    """RAGService must retrieve using the expanded query, not the raw user
    message — that's the whole point of the query-expansion stage."""

    async def fake_expand_query(user_message, chat_history, model=None):
        return RetrievalQuery(query="Inward Light discernment leadings", topics=["Inward Light"])

    monkeypatch.setattr("app.rag_service.expand_query", fake_expand_query)
    store = StubVectorStore(two_results)
    service = RAGService(store)

    await service.get_relevant_context("what should I do?")

    assert store.last_query == "Inward Light discernment leadings"


@pytest.mark.asyncio
async def test_get_relevant_context_uses_reranked_results_not_raw_candidates(
    two_results, monkeypatch
):
    """RAGService must build context/citations from rerank's output, not the
    unfiltered candidate list — otherwise reranking has no effect."""

    async def fake_rerank(query, candidates, keep_k, model=None):
        return [candidates[1]]  # keep only "Let your lives preach."

    monkeypatch.setattr("app.rag_service.rerank", fake_rerank)
    service = RAGService(StubVectorStore(two_results))

    context, citations, _ = await service.get_relevant_context("let your lives preach")

    assert len(citations) == 1
    assert "Let your lives preach." in context
    assert "discerned" not in context


@pytest.mark.asyncio
async def test_prepare_messages_with_sources_includes_topics_note(two_results, monkeypatch):
    async def fake_expand_query(user_message, chat_history, model=None):
        return RetrievalQuery(query=user_message, topics=["Inward Light", "convincement"])

    monkeypatch.setattr("app.rag_service.expand_query", fake_expand_query)
    service = RAGService(StubVectorStore(two_results))

    messages, _ = await service.prepare_messages_with_sources(
        system_prompt="Be a Quakerly companion.",
        chat_history=[],
        user_message="What did Fox say about discernment?",
    )

    assert "Related topics considered: Inward Light, convincement" in messages[0].content


@pytest.mark.asyncio
async def test_prepare_messages_with_sources_omits_topics_note_when_empty(two_results):
    service = RAGService(StubVectorStore(two_results))

    messages, _ = await service.prepare_messages_with_sources(
        system_prompt="Be a Quakerly companion.",
        chat_history=[],
        user_message="Hi",
    )

    assert "Related topics considered" not in messages[0].content
