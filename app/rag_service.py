import os
from typing import List, Optional, Tuple

from app.vector_store import VectorStore
from app.chat_gpt_client import Message, MessageRole
from app.models import RagCitation
from app.query_expansion import expand_query
from app.reranker import rerank

RETRIEVAL_CANDIDATE_K = int(os.getenv("RETRIEVAL_CANDIDATE_K", "12"))
RETRIEVAL_FINAL_K = int(os.getenv("RETRIEVAL_FINAL_K", "5"))


class RAGService:
    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store

    async def get_relevant_context(
        self,
        query: str,
        chat_history: Optional[List[Message]] = None,
        top_k: int = RETRIEVAL_FINAL_K,
    ) -> Tuple[str, List[RagCitation], List[str]]:
        expanded = await expand_query(query, chat_history or [])
        candidates = await self.vector_store.query(expanded.query, RETRIEVAL_CANDIDATE_K)
        results = await rerank(expanded.query, candidates, keep_k=top_k)

        context = "\n\n".join(
            [
                f"Source: {result.metadata.title or result.metadata.source}"
                f"{f' by {result.metadata.authors}' if result.metadata.authors else ''}\n"
                f"{result.content}"
                for result in results
            ]
        )
        citations = [
            RagCitation(
                source=result.metadata.source,
                content=result.content,
                title=result.metadata.title,
                authors=result.metadata.authors,
            )
            for result in results
        ]
        return context, citations, expanded.topics

    async def prepare_messages_with_sources(
        self, system_prompt: str, chat_history: List[Message], user_message: str
    ) -> Tuple[List[Message], List[RagCitation]]:
        context, citations, topics = await self.get_relevant_context(
            user_message, chat_history
        )

        prepared_messages = [
            Message(
                role=MessageRole.system,
                content=f"{system_prompt}\n\nRelevant context: {context}{_topics_note(topics)}",
            ),
            *chat_history,
            Message(role=MessageRole.user, content=user_message),
        ]

        return prepared_messages, citations

    # Keep the original prepare_messages method for backwards compatibility
    async def prepare_messages(
        self, system_prompt: str, chat_history: List[Message], user_message: str
    ) -> List[Message]:
        context, _, topics = await self.get_relevant_context(user_message, chat_history)

        prepared_messages = [
            Message(
                role=MessageRole.system,
                content=f"{system_prompt}\n\nRelevant context: {context}{_topics_note(topics)}",
            ),
            *chat_history,
            Message(role=MessageRole.user, content=user_message),
        ]

        return prepared_messages


def _topics_note(topics: List[str]) -> str:
    if not topics:
        return ""
    return f"\nRelated topics considered: {', '.join(topics)}"
