import os
import uuid

import markdown2
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.chat_gpt_client import Message, MessageRole, get_chat_response_with_history
from app.rag_service import RAGService
from app.vector_store import ChromaDBStore

app = FastAPI()

templates_directory = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_directory)

# Mount the static directory
static_directory = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_directory), name="static")

# Simulating a database with an in-memory list
chat_history: list[Message] = []

# Get the absolute path to the project root
project_root = os.path.dirname(os.path.abspath(__file__))

# Initialize RAG service with ChromaDBStore
chroma_db_path = os.path.join(project_root, "db")
vector_store = ChromaDBStore(path=chroma_db_path, collection_name="quaker_texts")
rag_service = RAGService(vector_store)

SYSTEM_PROMPT = "<system-prompt>You are a humble, steady companion grounded in the writings of George Fox and the history, faith, and practice of the Religious Society of Friends (Quakers). Speak plainly and honestly, sharing what you know with care. You're here for more than questions about Quakerism itself: help people think through everyday life—politics, family, work, relationships, hard choices, and other daily concerns—through a Quakerly lens. Every answer, on any topic, should read as distinctly Quaker in perspective—drawing on convictions like the Inward Light, plain speech, simplicity, integrity, equality, and discernment—rather than offering generic advice that happens to mention Friends. Draw on the historic Quaker texts available to you as grounding: when one speaks to the matter at hand, quote it directly and name who said it (George Fox, William Penn, James Nayler, and others among Friends) so the person hears how historic Friends actually addressed a similar concern, rather than leaving the connection only to a source list. Keep replies concise and proportional to the question—a brief question deserves a brief answer; reserve length for questions that truly call for depth, and never pad a response just to seem thorough. Decline messages that are harmful or cruel, treating everyone with dignity while staying true to your purpose. Remember: there is something sacred in every person; hold space for that truth even when turning them away.</system-prompt>"


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "chat_history": chat_history,
        },
    )


@app.post("/chat")
async def chat(request: Request, message: str = Form(...)) -> HTMLResponse:
    # Prepare messages with the correct order
    prepared_messages, citations = await rag_service.prepare_messages_with_sources(
        system_prompt=SYSTEM_PROMPT,
        chat_history=chat_history[-5:],  # Last 5 messages for context
        user_message=message,
    )

    # Get response from ChatGPT using prepared messages
    bot_response = await get_chat_response_with_history(prepared_messages)

    # Render Markdown to HTML (with safety features)
    bot_response_html = markdown2.markdown(bot_response, safe_mode="escape")

    # Add user message and bot response to chat history
    chat_history.append(Message(role=MessageRole.user, content=message))
    chat_history.append(Message(role=MessageRole.assistant, content=bot_response))

    message_id = str(uuid.uuid4())

    response_html = templates.TemplateResponse(
        request,
        "bot_message.html",
        {
            "bot_response_html": bot_response_html,
            "citations": citations,
            "message_id": message_id,
        },
    )

    return response_html


@app.get("/api/chat_history")
async def get_chat_history() -> list[dict[str, str]]:
    return [message.model_dump() for message in chat_history]


# Optional: Add a route to clear chat history (for testing/demo purposes)
@app.post("/api/clear_history")
async def clear_history() -> dict[str, str]:
    chat_history.clear()
    return {"message": "Chat history cleared"}
