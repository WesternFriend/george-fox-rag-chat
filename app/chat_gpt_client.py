import uuid
from enum import Enum
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import os
import logging
from typing import List, NamedTuple


CHAT_GPT_DEFAULT_MODEL = os.getenv("CHAT_GPT_MODEL", "gpt-4o")
CHAT_GPT_DEFAULT_TEMPERATURE = float(os.getenv("CHAT_GPT_TEMPERATURE", "0.7"))
CHAT_GPT_DEFAULT_MAX_TOKENS = int(os.getenv("CHAT_GPT_MAX_TOKENS", "1500"))


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: MessageRole
    content: str
    tts_available: bool = False


class ChatCompletionResult(NamedTuple):
    content: str
    finish_reason: str


# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure OpenAI client
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
if not client.api_key:
    raise ValueError("OPENAI_API_KEY not found in environment variables")


async def get_chat_response_with_history(
    messages: List[Message],
    model: str = CHAT_GPT_DEFAULT_MODEL,
    temperature: float = CHAT_GPT_DEFAULT_TEMPERATURE,
    max_tokens: int = CHAT_GPT_DEFAULT_MAX_TOKENS,
) -> ChatCompletionResult:
    """
    Asynchronous function to get a chat response from OpenAI's ChatGPT, considering chat history.

    :param messages: The full message list to send, in order — including the
        system message. Callers (RAGService.prepare_messages_with_sources) are
        responsible for building that system message; this function doesn't own
        or inject one of its own, since a second, generic system message ahead
        of the real one would dilute its instructions.
    :param model: The GPT model to use
    :param temperature: Controls randomness (0 to 1)
    :param max_tokens: Maximum number of tokens in the response
    :return: The assistant's response content, plus the completion's finish_reason
    """
    try:
        # Sent as plain role/content dicts, not serialized Message objects — Message
        # now carries fields (id, tts_available) that have no place in an OpenAI
        # chat-completion request payload.
        full_messages = [{"role": msg.role.value, "content": msg.content} for msg in messages]
        response = await client.chat.completions.create(
            model=model,
            messages=full_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        return ChatCompletionResult(
            content=choice.message.content.strip(),
            finish_reason=choice.finish_reason,
        )
    except Exception as e:
        logger.error(f"OpenAI API error: {str(e)}")
        return ChatCompletionResult(
            content=f"I'm sorry, but I encountered an error: {str(e)}",
            finish_reason="error",
        )
