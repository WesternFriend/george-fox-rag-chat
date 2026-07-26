import logging
import os
from pathlib import Path
from typing import List

from pydantic import BaseModel

from app.chat_gpt_client import client
from app.vector_store import VectorStoreResult

RERANK_MODEL = os.getenv("RERANK_MODEL", "gpt-4o-mini")

_PROMPT_PATH = Path(__file__).parent / "prompts" / "rerank_prompt.md"
_RERANK_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

logger = logging.getLogger(__name__)


class RankedIndices(BaseModel):
    keep: List[int]


async def rerank(
    query: str,
    candidates: List[VectorStoreResult],
    keep_k: int,
    model: str = RERANK_MODEL,
) -> List[VectorStoreResult]:
    """Select and reorder the candidates genuinely relevant to `query`, capped at
    `keep_k`. The model returns indices into `candidates` rather than regenerated
    passage text, so the kept passages can't drift from the retrieved source.
    """
    if not candidates:
        return []

    numbered = "\n\n".join(f"[{i}] {c.content}" for i, c in enumerate(candidates))
    user_content = (
        f"Query: {query}\n\nCandidates:\n{numbered}\n\nReturn at most {keep_k} indices."
    )

    try:
        response = await client.chat.completions.parse(
            model=model,
            messages=[
                {"role": "system", "content": _RERANK_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format=RankedIndices,
        )
        parsed = response.choices[0].message.parsed
    except Exception as e:
        logger.error(f"Rerank error: {str(e)}")
        parsed = None

    if parsed is None:
        return candidates[:keep_k]

    kept = [candidates[i] for i in parsed.keep[:keep_k] if 0 <= i < len(candidates)]
    return kept
