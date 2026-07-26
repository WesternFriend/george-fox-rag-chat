import logging
import os
from pathlib import Path
from typing import List

from pydantic import BaseModel

from app.chat_gpt_client import Message, client

QUERY_EXPANSION_MODEL = os.getenv("QUERY_EXPANSION_MODEL", "gpt-4o-mini")

_PROMPT_PATH = Path(__file__).parent / "prompts" / "query_expansion_prompt.md"
_QUERY_EXPANSION_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

logger = logging.getLogger(__name__)


class RetrievalQuery(BaseModel):
    query: str
    topics: List[str]


async def expand_query(
    user_message: str,
    chat_history: List[Message],
    model: str = QUERY_EXPANSION_MODEL,
) -> RetrievalQuery:
    """Rewrite a user message into a retrieval query enriched with the historic
    Quaker vocabulary the source texts actually use, plus the topics that
    enrichment drew on. Recent history is included only so pronouns/context in
    the current message can be resolved, not to change what's being asked.
    """
    history_text = "\n".join(f"{m.role.value}: {m.content}" for m in chat_history[-4:])
    user_content = (
        f"Recent conversation:\n{history_text}\n\nCurrent message: {user_message}"
        if history_text
        else f"Current message: {user_message}"
    )

    try:
        response = await client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": _QUERY_EXPANSION_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format=RetrievalQuery,
        )
        parsed = response.choices[0].message.parsed
    except Exception as e:
        logger.error(f"Query expansion error: {str(e)}")
        parsed = None

    if parsed is None:
        return RetrievalQuery(query=user_message, topics=[])
    return parsed
