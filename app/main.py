import os
from fastapi import FastAPI, Form, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from typing import List, Dict
import markdown2
import uuid

from app.chat_gpt_client import get_chat_response_with_history, Message, MessageRole
from app.rag_service import RAGService
from app.vector_store import ChromaDBStore

app = FastAPI()

templates_directory = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_directory)

# Mount the static directory
static_directory = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_directory), name="static")

# Simulating a database with an in-memory list
chat_history: List[Message] = []

# Get the absolute path to the project root
project_root = os.path.dirname(os.path.abspath(__file__))

# Initialize RAG service with ChromaDBStore
chroma_db_path = os.path.join(project_root, "db")
vector_store = ChromaDBStore(path=chroma_db_path, collection_name="prompt_engineering")
rag_service = RAGService(vector_store)

SYSTEM_PROMPT = "<system-prompt>You are a friendly, helpful assistant. Your main focus is the writings of George Fox and the history, faith, and practice of Quakers. You are knowledgeable about the Quaker movement and its teachings. You are here to answer questions and provide information about Quakerism. If the conversation strays from the topic of Quakerism, you can gently guide it back. Do not respond to messages that are inappropriate, offensive, or off-topic.</system-prompt>"


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
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
        "bot_message.html",
        {
            "request": request,
            "bot_response_html": bot_response_html,
            "citations": citations,
            "message_id": message_id,
        },
    )

    return response_html


@app.get("/api/chat_history")
async def get_chat_history() -> List[Dict[str, str]]:
    return [message.model_dump() for message in chat_history]


# Optional: Add a route to clear chat history (for testing/demo purposes)
@app.post("/api/clear_history")
async def clear_history() -> Dict[str, str]:
    chat_history.clear()
    return {"message": "Chat history cleared"}
