from starlette.requests import Request
from starlette.responses import Response

import app.session as session_module
from app.chat_gpt_client import Message, MessageRole
from app.session import (
    SESSION_COOKIE_NAME,
    SessionConfig,
    SessionStore,
    issue_session_id,
    rate_limit_key,
    read_session_id,
    set_session_cookie,
)


def _request_with_cookie(cookie_header):
    headers = []
    if cookie_header is not None:
        headers.append((b"cookie", cookie_header.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def _signed_cookie():
    """Returns (session_id, 'name=value' cookie header) for a freshly issued session."""
    session_id = issue_session_id()
    response = Response()
    set_session_cookie(response, session_id)
    cookie_value = response.headers["set-cookie"].split(";", 1)[0]
    return session_id, cookie_value


# --- cookie sign/read/tamper/expiry ---------------------------------------


def test_read_session_id_returns_none_when_no_cookie():
    assert read_session_id(_request_with_cookie(None)) is None


def test_read_session_id_round_trips_a_valid_cookie():
    session_id, cookie_value = _signed_cookie()
    assert read_session_id(_request_with_cookie(cookie_value)) == session_id


def test_read_session_id_rejects_a_tampered_cookie():
    _, cookie_value = _signed_cookie()
    name, value = cookie_value.split("=", 1)
    # Flip a character in the middle, not the last one or two — the tail of a
    # base64url string can include padding bits, so some replacements there
    # don't actually change the decoded bytes (flaky false negative). A
    # mid-string byte has no such padding, so any different character here is
    # guaranteed to invalidate the signature.
    index = len(value) // 2
    replacement = "A" if value[index] != "A" else "B"
    tampered_value = value[:index] + replacement + value[index + 1 :]
    tampered = f"{name}={tampered_value}"
    assert read_session_id(_request_with_cookie(tampered)) is None


def test_read_session_id_treats_expired_cookie_as_no_session(monkeypatch):
    _, cookie_value = _signed_cookie()
    # itsdangerous timestamps have 1-second resolution, so max_age=0 wouldn't
    # reliably be "already expired" if signing and reading land in the same
    # second — -1 guarantees any age (including 0) counts as expired.
    monkeypatch.setattr(session_module, "SESSION_TTL_SECONDS", -1)
    assert read_session_id(_request_with_cookie(cookie_value)) is None


def test_set_session_cookie_sets_expected_attributes():
    response = Response()
    set_session_cookie(response, "some-id")
    header = response.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in header
    assert "HttpOnly" in header
    assert "samesite=lax" in header.lower()


def test_rate_limit_key_uses_session_id_when_cookie_present():
    session_id, cookie_value = _signed_cookie()
    assert rate_limit_key(_request_with_cookie(cookie_value)) == session_id


def test_rate_limit_key_falls_back_to_client_ip_without_a_session():
    assert rate_limit_key(_request_with_cookie(None)) == "127.0.0.1"


# --- SessionStore ------------------------------------------------------------


def test_append_message_truncates_oldest_beyond_max_messages():
    store = SessionStore(SessionConfig(max_messages=2, max_sessions=10, ttl_seconds=3600))
    now = 1000.0
    oldest = Message(role=MessageRole.user, content="one")
    middle = Message(role=MessageRole.assistant, content="two")
    newest = Message(role=MessageRole.user, content="three")
    store.append_message("s1", oldest, now)
    store.append_message("s1", middle, now)
    store.append_message("s1", newest, now)

    state = store.get_or_create("s1", now)
    assert [m.content for m in state.chat_history] == ["two", "three"]
    assert oldest.id not in state.messages_by_id
    assert newest.id in state.messages_by_id


def test_evicts_least_recently_active_session_over_capacity():
    store = SessionStore(SessionConfig(max_messages=100, max_sessions=2, ttl_seconds=3600))
    store.get_or_create("s1", now=1.0)
    store.get_or_create("s2", now=2.0)
    store.get_or_create("s3", now=3.0)  # over capacity: evicts least-recently-active (s1)

    assert "s1" not in store._sessions
    assert "s2" in store._sessions
    assert "s3" in store._sessions


def test_get_or_create_refreshes_recency_so_it_survives_eviction():
    store = SessionStore(SessionConfig(max_messages=100, max_sessions=2, ttl_seconds=3600))
    store.get_or_create("s1", now=1.0)
    store.get_or_create("s2", now=2.0)
    store.get_or_create("s1", now=3.0)  # touching s1 again makes s2 the least-recent
    store.get_or_create("s3", now=4.0)  # evicts s2, not s1

    assert "s1" in store._sessions
    assert "s2" not in store._sessions


def test_sweep_expired_removes_only_idle_sessions():
    store = SessionStore(SessionConfig(max_messages=100, max_sessions=100, ttl_seconds=100))
    store.get_or_create("stale", now=0.0)
    store.get_or_create("fresh", now=150.0)

    store.sweep_expired(now=250.0)

    assert "stale" not in store._sessions
    assert "fresh" in store._sessions


def test_clear_empties_history_but_keeps_the_session_id():
    store = SessionStore(SessionConfig())
    now = 1.0
    store.append_message("s1", Message(role=MessageRole.user, content="hi"), now)

    store.clear("s1", now)

    state = store.get_or_create("s1", now)
    assert state.chat_history == []
    assert state.messages_by_id == {}
    assert state.session_id == "s1"


def test_two_sessions_never_share_history_or_message_ids():
    store = SessionStore(SessionConfig())
    now = 1.0
    msg = Message(role=MessageRole.assistant, content="hello")
    store.append_message("session-a", msg, now)

    state_b = store.get_or_create("session-b", now)

    assert state_b.chat_history == []
    assert msg.id not in state_b.messages_by_id
