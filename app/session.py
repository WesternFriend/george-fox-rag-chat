"""Per-visitor session identity and server-side session state.

Implements docs/specifications/session_isolation.md: an opaque, signed session-id
cookie (no payload beyond the id itself) backed by an in-memory, TTL/size-bounded
SessionStore. See that spec for the full rationale.
"""

from __future__ import annotations

import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from dotenv import load_dotenv
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.chat_gpt_client import Message

load_dotenv()  # idempotent; ensures SESSION_SECRET_KEY below is populated
# regardless of which module happens to import this one first.

ENVIRONMENT = os.getenv("ENVIRONMENT", "production")

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "gfrag_session")
SESSION_SECRET_KEY = os.environ["SESSION_SECRET_KEY"]
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_DAYS", "30")) * 86400
SESSION_MAX_MESSAGES = int(os.getenv("SESSION_MAX_MESSAGES", "100"))
SESSION_STORE_MAX_SESSIONS = int(os.getenv("SESSION_STORE_MAX_SESSIONS", "1000"))
CHAT_RATE_LIMIT = os.getenv("CHAT_RATE_LIMIT", "20/minute")
TTS_RATE_LIMIT = os.getenv("TTS_RATE_LIMIT", "10/minute")

# itsdangerous salt — not a secret, but named so signing and verification obviously
# share the same value rather than risking two inline literals drifting apart.
SESSION_COOKIE_SALT = "session-cookie"

# How often the periodic sweep (app/main.py lifespan) removes idle-expired sessions.
# Not spec-mandated as env-configurable; the exact interval isn't sensitive.
SESSION_SWEEP_INTERVAL_SECONDS = 1800

_serializer = URLSafeTimedSerializer(SESSION_SECRET_KEY, salt=SESSION_COOKIE_SALT)


def read_session_id(request: Request) -> str | None:
    """Return the caller's session id, or None if absent, tampered, or expired.

    Deliberately collapses "no cookie", "tampered cookie", and "expired cookie"
    into the same None result — the caller shouldn't be able to distinguish why a
    given cookie didn't validate.
    """
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    try:
        return _serializer.loads(raw, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def issue_session_id() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _serializer.dumps(session_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=ENVIRONMENT != "development",
        samesite="lax",
        path="/",
    )


@dataclass
class SessionConfig:
    ttl_seconds: int = SESSION_TTL_SECONDS
    max_messages: int = SESSION_MAX_MESSAGES
    max_sessions: int = SESSION_STORE_MAX_SESSIONS


@dataclass
class SessionState:
    session_id: str
    chat_history: list[Message] = field(default_factory=list)
    messages_by_id: dict[str, Message] = field(default_factory=dict)
    last_active: float = 0.0


class SessionStore:
    def __init__(self, config: SessionConfig):
        self._config = config
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()

    def get_or_create(self, session_id: str, now: float) -> SessionState:
        if session_id in self._sessions:
            self._sessions.move_to_end(session_id)
            state = self._sessions[session_id]
            state.last_active = now
            return state
        state = SessionState(session_id=session_id, last_active=now)
        self._sessions[session_id] = state
        self._evict_if_over_capacity()
        return state

    def append_message(self, session_id: str, message: Message, now: float) -> None:
        state = self.get_or_create(session_id, now)
        state.chat_history.append(message)
        state.messages_by_id[message.id] = message
        if len(state.chat_history) > self._config.max_messages:
            oldest = state.chat_history.pop(0)
            state.messages_by_id.pop(oldest.id, None)

    def clear(self, session_id: str, now: float) -> None:
        """Clear a session's history/messages, keeping the same session id."""
        state = self.get_or_create(session_id, now)
        state.chat_history.clear()
        state.messages_by_id.clear()

    def sweep_expired(self, now: float) -> None:
        """Proactively remove idle-expired sessions, independent of eviction-at-capacity."""
        expired = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_active > self._config.ttl_seconds
        ]
        for sid in expired:
            del self._sessions[sid]

    def _evict_if_over_capacity(self) -> None:
        while len(self._sessions) > self._config.max_sessions:
            self._sessions.popitem(last=False)  # evict least-recently-active


session_store = SessionStore(SessionConfig())


def get_session(request: Request) -> SessionState:
    """FastAPI dependency: resolves the caller's SessionState.

    Doesn't set the response cookie itself — a route can return an arbitrary
    Response subclass (TemplateResponse, etc.) that FastAPI won't merge an
    injected Response's headers into, so cookie-setting is done uniformly by
    `session_cookie_middleware` instead, keyed off `request.state.session_id`
    stashed here.
    """
    session_id = read_session_id(request)
    if session_id is None:
        session_id = issue_session_id()
    request.state.session_id = session_id
    return session_store.get_or_create(session_id, time.time())


def rate_limit_key(request: Request) -> str:
    """slowapi key function: per-session where a session is known, else per-IP.

    A session is the real identity boundary once one exists (session_isolation.md
    §6) — IP both under- and over-counts relative to actual users behind shared
    connections/NAT. Falls back to IP only for a request with no session context
    yet (e.g. the very first request from a new visitor).
    """
    session_id = read_session_id(request)
    if session_id is not None:
        return session_id
    return request.client.host if request.client else "unknown"
