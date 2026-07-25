import pytest
from fastapi.testclient import TestClient
from app.chat_gpt_client import ChatCompletionResult
from app.main import app, TTS_ERROR_MESSAGE_NOT_FOUND
from bs4 import BeautifulSoup

client = TestClient(app)

# Mock data
mock_rag_citations = [
    {"source": "Source 1", "content": "Content 1"},
    {"source": "Source 2", "content": "Content 2"},
]

mock_chat_response = "This is a mock response from the LLM."


# Mock functions
async def mock_prepare_messages_with_sources(*args, **kwargs):
    return [], mock_rag_citations


async def mock_get_chat_response_with_history(*args, **kwargs):
    return ChatCompletionResult(content=mock_chat_response, finish_reason="stop")


@pytest.fixture
def mock_services(monkeypatch):
    # Mock RAG service
    monkeypatch.setattr(
        "app.main.rag_service.prepare_messages_with_sources",
        mock_prepare_messages_with_sources,
    )

    # Mock LLM service (get_chat_response_with_history)
    monkeypatch.setattr(
        "app.main.get_chat_response_with_history", mock_get_chat_response_with_history
    )


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    # The limiter's storage is a process-global singleton — without resetting it,
    # unrelated tests sharing a TestClient/session cookie could trip each other's
    # rate limits.
    app.state.limiter.reset()
    yield


def _tts_button(html: bytes):
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("button", class_="tts-toggle") or soup.find(
        "button", attrs={"aria-label": lambda v: v and "Audio unavailable" in v}
    )


def test_sources_toggle_independent(mock_services):
    messages = [
        "Tell me about prompt engineering",
        "What are some key aspects of prompt engineering?",
        "How is prompt engineering used in AI?",
    ]

    all_ids = []
    for i, message in enumerate(messages):
        response = client.post("/chat", data={"message": message})
        assert (
            response.status_code == 200
        ), f"Failed to get response for message: {message}"

        soup = BeautifulSoup(response.content, "html.parser")
        toggles = soup.find_all("button", attrs={"data-bs-toggle": "collapse"})

        print(f"Number of toggles found for message '{message}': {len(toggles)}")

        for toggle in toggles:
            print(f"Toggle attributes:")
            for attr, value in toggle.attrs.items():
                print(f"  {attr}: {value}")

            target = toggle.get("data-bs-target")
            if target:
                id_match = target.split("-")[-1]  # Assuming format '#sources-<id>'
                all_ids.append(id_match)
                print(f"ID found: {id_match}")
            else:
                print("Toggle found without data-bs-target attribute")

        # Print the entire HTML content for debugging
        print(f"Full HTML content for message {i + 1}:")
        print(soup.prettify())
        print("\n" + "=" * 50 + "\n")

    unique_ids = set(all_ids)
    print(f"All IDs found: {all_ids}")
    print(f"Unique IDs found: {unique_ids}")

    assert (
        len(unique_ids) > 1
    ), f"Expected multiple unique IDs, but found {len(unique_ids)}: {unique_ids}"
    assert len(unique_ids) == len(
        all_ids
    ), f"Some IDs are not unique. All IDs: {all_ids}, Unique IDs: {unique_ids}"


# --- session isolation --------------------------------------------------


def test_two_sessions_do_not_share_chat_history(mock_services):
    client_a = TestClient(app)
    client_b = TestClient(app)

    client_a.post("/chat", data={"message": "hello from A"})

    history_a = client_a.get("/api/chat_history").json()
    history_b = client_b.get("/api/chat_history").json()

    assert any(m["content"] == "hello from A" for m in history_a)
    assert history_b == []


def test_clear_history_only_clears_the_callers_session(mock_services):
    client_a = TestClient(app)
    client_b = TestClient(app)

    client_a.post("/chat", data={"message": "hello from A"})
    client_b.post("/chat", data={"message": "hello from B"})

    client_a.post("/api/clear_history")

    assert client_a.get("/api/chat_history").json() == []
    assert len(client_b.get("/api/chat_history").json()) == 2


# --- tts_available / disabled button rendering ---------------------------


def test_short_response_has_enabled_listen_button(mock_services):
    response = client.post("/chat", data={"message": "hi"})
    button = _tts_button(response.content)
    assert button is not None
    assert button.get("disabled") is None
    assert button.get("data-message-id")


def test_long_response_has_disabled_listen_button(monkeypatch):
    long_response = "x" * 4000  # longer than the default TTS_MAX_INPUT_CHARS (3800)

    async def long_mock(*args, **kwargs):
        return ChatCompletionResult(content=long_response, finish_reason="stop")

    monkeypatch.setattr(
        "app.main.rag_service.prepare_messages_with_sources",
        mock_prepare_messages_with_sources,
    )
    monkeypatch.setattr("app.main.get_chat_response_with_history", long_mock)

    response = client.post("/chat", data={"message": "tell me a long story"})
    soup = BeautifulSoup(response.content, "html.parser")
    button = soup.find("button", attrs={"aria-label": lambda v: v and "Audio unavailable" in v})

    assert button is not None
    assert button.get("disabled") is not None


# --- POST /api/messages/{message_id}/speech -------------------------------


def _post_chat_and_get_message_id(post_client, mock_synthesize=None, message="hi"):
    response = post_client.post("/chat", data={"message": message})
    button = _tts_button(response.content)
    return button["data-message-id"]


def test_speech_endpoint_happy_path(mock_services, monkeypatch):
    calls = []

    async def fake_synthesize_speech(text):
        calls.append(text)
        return b"fake-mp3-bytes"

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize_speech)

    local_client = TestClient(app)
    message_id = _post_chat_and_get_message_id(local_client)

    response = local_client.post(f"/api/messages/{message_id}/speech")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake-mp3-bytes"
    assert calls == [mock_chat_response]  # sent unmodified — no transformation


def test_speech_endpoint_404_for_unknown_message_id(mock_services):
    local_client = TestClient(app)
    local_client.post("/chat", data={"message": "hi"})  # establishes a session cookie

    response = local_client.post("/api/messages/does-not-exist/speech")

    assert response.status_code == 404
    assert response.json() == {"detail": TTS_ERROR_MESSAGE_NOT_FOUND}


def test_speech_endpoint_404_for_message_in_a_different_session(mock_services):
    client_a = TestClient(app)
    client_b = TestClient(app)

    message_id = _post_chat_and_get_message_id(client_a)

    response_a_unknown = client_a.post("/api/messages/does-not-exist/speech")
    response_b_cross_session = client_b.post(f"/api/messages/{message_id}/speech")

    assert response_b_cross_session.status_code == 404
    assert response_b_cross_session.json() == response_a_unknown.json()


def test_speech_endpoint_413_short_circuits_before_tts_service(monkeypatch):
    long_response = "x" * 4000

    async def long_mock(*args, **kwargs):
        return ChatCompletionResult(content=long_response, finish_reason="stop")

    monkeypatch.setattr(
        "app.main.rag_service.prepare_messages_with_sources",
        mock_prepare_messages_with_sources,
    )
    monkeypatch.setattr("app.main.get_chat_response_with_history", long_mock)

    calls = []

    async def fake_synthesize_speech(text):
        calls.append(text)
        return b"should-not-be-called"

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize_speech)

    local_client = TestClient(app)
    chat_response = local_client.post("/chat", data={"message": "tell me a long story"})
    soup = BeautifulSoup(chat_response.content, "html.parser")
    disabled_button = soup.find(
        "button", attrs={"aria-label": lambda v: v and "Audio unavailable" in v}
    )
    # The disabled button has no data-message-id — recover the id from the citation
    # panel instead, purely to exercise the route directly in this test.
    sources_button = soup.find("button", attrs={"data-bs-toggle": "collapse"})
    message_id = sources_button["data-bs-target"].removeprefix("#sources-")

    response = local_client.post(f"/api/messages/{message_id}/speech")

    assert response.status_code == 413
    assert calls == []


def test_speech_endpoint_rate_limited(mock_services, monkeypatch):
    async def fake_synthesize_speech(text):
        return b"fake-mp3-bytes"

    monkeypatch.setattr("app.tts_service.synthesize_speech", fake_synthesize_speech)

    local_client = TestClient(app)
    message_id = _post_chat_and_get_message_id(local_client)

    # TTS_RATE_LIMIT default is 10/minute; the 11th request in the window 429s.
    statuses = [
        local_client.post(f"/api/messages/{message_id}/speech").status_code
        for _ in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


# --- audio_mode wiring -----------------------------------------------------


def test_audio_mode_adds_addendum_and_uses_audio_max_tokens(monkeypatch):
    captured_system_prompts = []
    captured_max_tokens = []

    async def capturing_prepare_messages(system_prompt, chat_history, user_message):
        captured_system_prompts.append(system_prompt)
        return [], mock_rag_citations

    async def capturing_get_chat_response(*args, **kwargs):
        captured_max_tokens.append(kwargs.get("max_tokens"))
        return ChatCompletionResult(content=mock_chat_response, finish_reason="stop")

    monkeypatch.setattr(
        "app.main.rag_service.prepare_messages_with_sources", capturing_prepare_messages
    )
    monkeypatch.setattr("app.main.get_chat_response_with_history", capturing_get_chat_response)

    local_client = TestClient(app)
    local_client.post("/chat", data={"message": "hi", "audio_mode": "true"})

    from app.main import AUDIO_MODE_ADDENDUM, CHAT_GPT_MAX_TOKENS_AUDIO

    assert AUDIO_MODE_ADDENDUM in captured_system_prompts[0]
    assert captured_max_tokens[0] == CHAT_GPT_MAX_TOKENS_AUDIO


def test_audio_mode_omitted_field_behaves_like_unchecked(monkeypatch):
    captured_system_prompts = []
    captured_max_tokens = []

    async def capturing_prepare_messages(system_prompt, chat_history, user_message):
        captured_system_prompts.append(system_prompt)
        return [], mock_rag_citations

    async def capturing_get_chat_response(*args, **kwargs):
        captured_max_tokens.append(kwargs.get("max_tokens"))
        return ChatCompletionResult(content=mock_chat_response, finish_reason="stop")

    monkeypatch.setattr(
        "app.main.rag_service.prepare_messages_with_sources", capturing_prepare_messages
    )
    monkeypatch.setattr("app.main.get_chat_response_with_history", capturing_get_chat_response)

    local_client = TestClient(app)
    # audio_mode field omitted entirely, not sent as "false"
    local_client.post("/chat", data={"message": "hi"})

    from app.main import AUDIO_MODE_ADDENDUM, CHAT_GPT_DEFAULT_MAX_TOKENS

    assert AUDIO_MODE_ADDENDUM not in captured_system_prompts[0]
    assert captured_max_tokens[0] == CHAT_GPT_DEFAULT_MAX_TOKENS


# --- config validation -------------------------------------------------


def test_validate_config_rejects_out_of_range_speed(monkeypatch):
    import app.tts_client as tts_client
    from app.main import validate_config

    monkeypatch.setattr(tts_client, "TTS_SPEED", 10.0)

    with pytest.raises(RuntimeError):
        validate_config()


def test_validate_config_accepts_default_settings():
    from app.main import validate_config

    validate_config()  # should not raise
