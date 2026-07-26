# Read-Aloud (Text-to-Speech) Specification

Status: implemented (see `app/tts_client.py`, `app/tts_service.py`, `app/static/tts.js`, and
the `POST /api/messages/{message_id}/speech` route in `app/main.py`). Retained here as the
design record — sections below describe the shipped behavior except where marked otherwise
(e.g. §10's open questions, still open).

## 1. Purpose

Bot responses are currently text-only. This spec adds an accessibility feature that lets
a user hear a bot response read aloud, using OpenAI's speech-generation API
([`/v1/audio/speech`](https://developers.openai.com/api/docs/guides/text-to-speech)). This
helps users with low vision, reading disorders, or a general preference for listening, and
complements (doesn't replace) screen-reader support, which already works on the existing
HTML.

## 2. API choice

| Setting | Value | Why |
|---|---|---|
| Model | `gpt-4o-mini-tts` | Current recommended model; supports `instructions` for tone/style control, which `tts-1`/`tts-1-hd` do not. LLM-based, not a character-level engine — see §4.2 for why that matters to this spec's design. |
| Voice | `cedar` | One of the two newer voices (`cedar`, `marin`) added alongside `gpt-4o-mini-tts`; only available on that model, not `tts-1`/`tts-1-hd`. |
| Speed | `1.0` | Default rate; configurable (see §6). |
| Output format | `mp3`, fixed | Universally supported by `<audio>` in all target browsers. Not configurable — see §4.3 for why a configurable format would create a response `media_type` mismatch. |

The endpoint is `POST https://api.openai.com/v1/audio/speech`, called via the `openai`
Python SDK already used by
[`app/chat_gpt_client.py`](../../app/chat_gpt_client.py). Synchronous, non-streaming —
see §10 for streaming as a future option.

**Pricing.** At the time this specification was written, the published rate for
`gpt-4o-mini-tts` output audio was **$12.00 per 1M tokens** (`openai.com/api/pricing`),
metered on generated audio, not input text length. Treat this as a snapshot, not a fact
baked into the architecture — confirm current pricing before release and track actual
spend through OpenAI's own billing/usage dashboard rather than this document.

Response length is the main lever this spec controls (§4.5), but it isn't the only cost
variable in practice: how often users actually choose to play a response, duplicate/retry
requests, active user count, and cache retention all move the total. §4.4's cache and §8's
rate limiting address the request-level variables; §4.5 addresses response length.

**Input length limit.** OpenAI's own documentation states the `input` field has a maximum
length, cited inconsistently across sources as roughly 4,096 characters in some places and
~2,000 tokens in others — these aren't the same measure (2,000 tokens is closer to 8,000
characters of English prose), so don't trust either number blindly. Pin the actual boundary
down empirically against the live API during implementation (send a long string, observe
where it starts rejecting) rather than coding to an unverified constant. §4.2 handles this
with a conservative, configurable margin either way. This matters because Listen is
available on *every* bot response (§3), including long ones generated with audio mode off
— the limit isn't a corner case restricted to §4.5's brevity path. This is a length limit
only, unrelated to formatting — see §4.2 for why formatting itself isn't a concern here.

**AI-voice disclosure.** OpenAI's usage policies for voice output require that end users be
clearly informed the voice is AI-generated, not a human recording. This isn't optional
polish — see §3 for the visible (not just `aria-label`) text this requires. Verify the
exact required wording against OpenAI's current usage policies at implementation time;
design here assumes a disclosure requirement exists and is non-negotiable.

## 3. UX design

A "Listen" (🔊) button is added to each bot message, next to the existing "Toggle Sources"
button in [`bot_message.html`](../../app/templates/bot_message.html). Clicking it:

1. Shows a loading state ("Generating audio…") on the button.
2. `POST`s to the new backend endpoint (§4.3) to generate/retrieve audio for that message.
3. Plays it through an `<audio>` element scoped to that message.
4. While playing, the button becomes a "Stop" (⏹) control; clicking again pauses/resets
   playback. Only one message plays at a time — starting playback on a second message
   stops any message already playing.

A response known at render time to be too long for TTS (§4.2 — determined eagerly, when the
response is generated, not discovered lazily on click) renders its Listen button in a
disabled state with an explanatory `title`/`aria-label`, rather than letting the user click
it and receive a `413` after a round trip.

A click starts the request, but actual playback happens only after a server round trip
that can take a few seconds (synthesis isn't instant). Browsers vary in whether they still
treat playback as "within" the original user gesture after that delay — this is a real,
not theoretical, risk on some platforms (iOS Safari in particular), not something the
original click fully neutralizes. Test across target browsers as part of §9 rather than
assuming the click alone settles it.

Visible, not hidden, disclosure text near the audio controls (first appearance, or a
persistent small caption near the audio-mode toggle):

> Spoken audio is generated by an AI voice, not a human recording.

Accessibility details:

- The button gets an explicit `aria-label` (e.g. `"Read this response aloud"` /
  `"Stop reading aloud"`, updated as state changes), not just the emoji glyph. Prefer an
  icon (inline SVG or an existing icon set, `aria-hidden="true"`) plus visible text over
  relying on the emoji glyph alone — emoji rendering and assistive-tech verbosity vary by
  platform. Non-blocking, but do this if convenient.
- A visually-hidden `aria-live="polite"` region per message announces state changes
  ("Generating audio…", "Playing audio", "This response is too long to read aloud",
  "Audio unavailable") for screen-reader users, since the button's own label change isn't
  reliably announced on its own.
- **Auto-play-on-arrival, implemented** (superseding the "not in this iteration" framing
  originally here — see §10 open question 1, now resolved): tied specifically to the
  audio-mode checkbox (§3.1), not a separate always-on toggle. On every `htmx:afterSwap`,
  if the checkbox is checked *at that moment* (re-checked per arrival, not cached from send
  time) and the newly arrived message has an enabled Listen button, playback starts without
  a click. This sidesteps the "reading over you while you're still typing" concern raised
  below, since it only fires for a user who has explicitly opted into listening. The
  autoplay-policy risk is real but not new — it already existed for the click-triggered path
  (see the note above this list about the gesture "expiring" during the network round trip)
  and is handled the same way: a rejected `play()` promise surfaces as the existing ERROR
  state rather than a crash or a silent no-op.

### 3.1 Audio mode toggle

A checkbox near the message input, labeled **"Prefer shorter replies for listening"**
(not "audio-friendly replies" or similar — that wording read as if it enabled audio
playback itself, when what it actually does is bias generation length; see §4.5), lets a
user opt in *before* sending a message rather than deciding per-response. When checked:

- It's sent with each `/chat` POST as `audio_mode`. Since HTML omits unchecked checkboxes
  from form submission entirely (there is no `audio_mode=false`), the backend must treat
  *presence* of the field as true and *absence* as false — not parse a boolean value out
  of it. FastAPI's `Form(False)` default handles the absent case correctly; still worth an
  explicit test (§9) rather than assuming.
- It lives inside the same `<form>` the chat input already posts via `hx-post="/chat"`,
  so HTMX includes it automatically with no extra `hx-include` wiring. That form is never
  itself an HTMX swap target (only `#chat-container` is, per `chat.html`), so the checkbox
  survives every chat exchange without needing to be re-attached to new DOM.
- State is persisted client-side only (`localStorage`, restored on page load), consistent
  with this app's no-server-persistence design — it is not part of `chat_history`.
- It signals the backend to bias generation toward shorter responses (§4.5) for every
  message sent while checked. This has to happen at generation time, not as a
  per-message afterthought: by the time a response already exists, it's too late to make
  it shorter without discarding and regenerating it.

This also gives a natural signal for auto-play (§10 open question 1): a user who has
explicitly opted into shorter replies has told the app they intend to listen, which is a
stronger basis for auto-playing than guessing on every message regardless of intent.

## 4. Backend design

Two clearly separated phases — a cheap, local one that always runs, and an expensive,
billed one that only runs on demand:

```text
POST /chat  (existing route)
        │
        ▼
  chat completion returns bot_response (raw Markdown text from the LLM)
        │
        ▼
  markdown2.markdown(bot_response) → bot_response_html    [on-screen display, unchanged]
        │
        ▼
  tts_available = len(bot_response) <= TTS_MAX_INPUT_CHARS   [§4.2 — a length check
        │                                                      only; no text transformation]
        ▼
  stored in the caller's session (session_isolation.md §4) — template renders an
  enabled Listen button, or a disabled one with an explanation, based on tts_available


POST /api/messages/{message_id}/speech   (this spec's new route)
        │
        ▼
  rate limit, keyed by session (§8)
        │
        ▼
  look up message in the caller's session's messages_by_id — 404 if missing (wrong
  session or truly unknown are indistinguishable on purpose — session_isolation.md §5)
        │
        ▼
  message.tts_available? no → 413 immediately, no TTSService call needed at all
        │ yes
        ▼
  TTSService.get_or_generate(session_id, message_id, message.content):
    cache hit  → return cached bytes
    cache miss → per-(session, message) lock, semaphore,
                 synthesize_speech() with timeout      [§4.1, §4.4]
        │
        ▼
  200 audio/mpeg  (or a mapped error status — §4.3)
```

The point of the eager/lazy split: the length check (§4.2) is free and local — no reason to
defer it to the moment someone clicks Listen. What has to stay lazy is only the *billed*
step, `synthesize_speech`. Deferring the check too would cost real UX for no benefit: a user
could click Listen, wait for a round trip, and only then discover the response was too
long — with the split above, that's known and shown *before* the click even happens.

### 4.1 `app/tts_client.py` — thin API wrapper

Mirrors [`app/chat_gpt_client.py`](../../app/chat_gpt_client.py)'s shape. Deliberately thin
— no caching, no ownership checks, no text preparation here; those live in `TTSService`
(§4.4) so this module stays a small, directly testable wrapper around the SDK call.

```python
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.getenv("TTS_VOICE", "cedar")
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
TTS_TIMEOUT_SECONDS = float(os.getenv("TTS_TIMEOUT_SECONDS", "30"))
AUDIO_RESPONSE_FORMAT = "mp3"  # fixed, not configurable — see §2, §4.3


async def synthesize_speech(text: str) -> bytes:
    """Call OpenAI's speech endpoint and return raw MP3 bytes, or raise."""
    async with client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        speed=TTS_SPEED,
        response_format=AUDIO_RESPONSE_FORMAT,
        timeout=TTS_TIMEOUT_SECONDS,
    ) as response:
        return await response.read()
```

**Shipped, confirmed access pattern** — this superseded an earlier draft's `response.content`
guess. OpenAI's own current documentation for this endpoint uses the SDK's
`with_streaming_response` context-manager form; `await response.read()` inside it returns the
full `bytes` payload, confirmed against the `openai` version pinned in `pyproject.toml`. Still
fully buffers server-side before returning — this app doesn't stream audio through to the
browser (§10 notes that as a future option) — so it's behaviorally equivalent to the original
design, just using the API surface OpenAI's docs actually recommend.

Doesn't swallow exceptions into a string return value, unlike
`get_chat_response_with_history` — the caller needs to distinguish failure modes to map
them to the right HTTP status (§4.3).

### 4.2 Input length check — and why there's no text transformation at all

**This section previously specified three layers of Markdown mitigation** — a system-prompt
instruction against formatting, a `markdown-it-py` structural parser converting Markdown to
linear spoken prose, and a character-allowlist safety net. All three were removed after
direct testing against the actual target model and voice (`gpt-4o-mini-tts`, `cedar`) in the
OpenAI TTS playground showed the premise they were built on doesn't hold for this specific
model:

| Input tested | Result |
|---|---|
| `**bold**`, `*italic*`, `` `inline code` `` | Clean — no asterisks/backticks read aloud |
| A Markdown link with a real URL | The URL is read cleanly as part of the sentence, not spelled out character-by-character |
| A fenced code block | Read clean enough; backticks not read |
| An ordered list | Steps read correctly |
| A blockquote | Sounds good |
| A pipe table | Reads fine; pipe characters not read literally |
| A stray `#` in ordinary prose ("Room #42") | Read correctly as "Room number forty-two" — the model disambiguated it from heading syntax using context |

That last result is evidence *against* the character-allowlist layer specifically, not just
neutral: a static allowlist would have deleted the `#` outright, turning "Room #42" into
"Room 42" and losing the "number" framing the model inferred correctly on its own. A blunt
filter can't be context-sensitive the way the model already is — the "safety net" would have
been a net loss of information in that case, not a safety improvement.

**Why the original design was wrong.** The three-layer design generalized from how
character-level/phonetic TTS engines handle markup — those do read formatting syntax
literally, and mitigating that assumption is a reasonable, commonly-recommended pattern for
that class of system. `gpt-4o-mini-tts` isn't that class of system: it's LLM-based, with
semantic understanding of the input, and it handles Markdown-formatted prose roughly the way
a person reading it aloud would. That should have been tested before three layers of
mitigation were designed around it, not after — recorded here so a future reader of this
spec's history understands why it changed shape, not just that it did.

**What's sent to the API**: `bot_response` — the LLM's raw output — unmodified. The exact
same string `markdown2` renders into `bot_response_html` for on-screen display, not a
derived version. This also resolves the quote-fidelity concern earlier drafts raised (§10):
with no transformation step at all, there is no mechanism left that could alter a verbatim
quotation's characters between what's shown and what's spoken.

**What's still needed — an input length check**, which is unrelated to formatting and still
real (§2):

```python
TTS_MAX_INPUT_CHARS = int(os.getenv("TTS_MAX_INPUT_CHARS", "3800"))  # margin below the API's real limit — pin exactly at implementation time, see §2
```

Computed once, in the `/chat` handler, directly against `bot_response`'s length —
`tts_available = len(bot_response) <= TTS_MAX_INPUT_CHARS` — not lazily inside the TTS
route (see the §4 diagram). Keeping this eager still buys real UX: a response known to be
too long renders a disabled Listen button immediately (§3), not after a round trip.

If oversized, `413`/disabled-button (§4.3) applies rather than truncating and serving a
partial clip — this app's system prompt requires verbatim, attributed quotes when a passage
is genuinely relevant, and a truncated excerpt risks cutting a quote or its attribution
mid-thought, which is worse than "no audio" for an accessibility feature. If usage shows
this firing often, chunked multi-clip playback is the real fix, not a silent truncate —
noted as a fast-follow in §10, deliberately not built here.

**Residual, honest caveat.** The table above is a smoke test, not exhaustive coverage —
seven short, largely independent examples of the highest-risk constructs, not deeply nested
structures, long tables, multiple consecutive links, or this app's specific archaic-English
quoted material interacting with formatting. §9's audio-quality corpus still exists, now
functioning as an ongoing regression check against real RAG output rather than as the
primary risk-mitigation mechanism it was designed as originally.

### 4.3 New route: `POST /api/messages/{message_id}/speech`

**`POST`, not `GET`, and this is a required design choice, not a style preference.** A
`GET` here looks read-only but isn't: on a cache miss it calls a paid external API, mutates
the audio cache, and can take several seconds. Browsers, crawlers, accessibility tools,
proxies, and speculative link-prefetchers can all issue GET requests without an intentional
Listen click — `preload="none"` on the `<audio>` element only reduces *ordinary* browser
prefetching; it does nothing about the rest of that list, and none of them should be able
to trigger a billed API call. `POST` also gives the frontend real access to structured
error bodies — a bare `<audio src="...">` load exposes only a coarse media error to JS, not
a 404 vs. 413 vs. 502 distinction.

Error-detail strings are named constants at module scope, not literals repeated inline —
see the convention note in §4.6:

```python
# --- app/tts_routes.py (or wherever this route lives) — constants, top of file ---
TTS_ERROR_MESSAGE_NOT_FOUND = "message not found"
TTS_ERROR_MESSAGE_TOO_LONG = "message too long for speech synthesis"
TTS_ERROR_GENERATION_FAILED = "speech generation failed"
TTS_ERROR_GENERATION_TIMED_OUT = "speech generation timed out"
TTS_ERROR_MISCONFIGURED = "speech generation is misconfigured"


@app.post("/api/messages/{message_id}/speech")
async def synthesize_message_speech(
    message_id: str,
    session: SessionState = Depends(get_session),  # session_isolation.md §3–4
):
    message = session.messages_by_id.get(message_id)
    if message is None:
        raise HTTPException(404, TTS_ERROR_MESSAGE_NOT_FOUND)
    if not message.tts_available:
        raise HTTPException(413, TTS_ERROR_MESSAGE_TOO_LONG)
    try:
        audio = await tts_service.get_or_generate(
            session.id, message_id, message.content
        )
    except TTSUpstreamTimeoutError:
        raise HTTPException(504, TTS_ERROR_GENERATION_TIMED_OUT)
    except TTSUpstreamError:
        raise HTTPException(502, TTS_ERROR_GENERATION_FAILED)
    return Response(content=audio, media_type="audio/mpeg")
```

Response contract (the exact strings below are these same constants — one source of truth,
not independently maintained copies):

```http
POST /api/messages/{message_id}/speech

200  Content-Type: audio/mpeg          — synthesis succeeded (cached or fresh)
404  {"detail": "message not found"}   — unknown message_id, or valid in a different
                                          session (session_isolation.md §5 — these are
                                          deliberately indistinguishable)
413  {"detail": "message too long for speech synthesis"}   — known at /chat time (§4.2)
429  {"detail": "rate limit exceeded"} — see §8
500  {"detail": "speech generation is misconfigured"}  — see §6, never in normal operation
502  {"detail": "speech generation failed"}   — upstream error, generic
504  {"detail": "speech generation timed out"} — see TTS_TIMEOUT_SECONDS, §4.1
```

Never forward a raw OpenAI exception message into the response body — log it server-side,
return the fixed strings above.

**Ownership.** Resolved via session-scoped lookup: `session.messages_by_id` is the caller's
own session's dict (`session_isolation.md` §4–5), not a global one, so a `message_id` that
exists but belongs to a different session simply isn't found here — see the 404 row above.

### 4.4 `TTSService` — state and concurrency control

State lives in a small service object, `app/tts_service.py`, rather than bare module-level
dicts in `app/main.py` — mirroring how [`RAGService`](../../app/rag_service.py) already
wraps `VectorStore` in this codebase. This is consistent with the app's existing shape, and
it keeps caching/locking/concurrency logic out of the route handler, which makes it
independently testable (§9) instead of only testable through the FastAPI route.

**`Message` gains one field**, computed once in the `/chat` handler:

```python
class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: MessageRole
    content: str
    tts_available: bool = False   # len(content) <= TTS_MAX_INPUT_CHARS (§4.2), assistant messages only
```

`default_factory`/defaults mean every existing `Message(role=..., content=...)` call site
(across `main.py`, `rag_service.py`, and the tests) keeps working unchanged — confirmed no
test does exact-shape/equality comparison on `Message` that a new field would break
(`tests/test_rag_service.py`, `tests/test_chat_gpt_client.py`). No separate "spoken text"
field — `content` *is* what's sent to TTS (§4.2), so there's nothing else to store.

In the `/chat` handler, after the chat completion returns:

```python
message_id = str(uuid.uuid4())
bot_message = Message(
    id=message_id,
    role=MessageRole.assistant,
    content=bot_response,
    tts_available=len(bot_response) <= TTS_MAX_INPUT_CHARS,
)
session.append_message(bot_message)  # session_isolation.md §4 — replaces the old global chat_history.append(...)
```

`session.messages_by_id` (populated by `SessionStore.append_message`,
`session_isolation.md` §4) is an **index** into the session's own `chat_history` — a
reference to the same `Message` object, not a duplicate of its content.

**`TTSService`**, keyed by `(session_id, message_id)` — required now that
[`session_isolation.md`](session_isolation.md) has settled on real session scoping, not a
hypothetical "if Option B" case:

```python
class TTSConfig:
    max_entries: int = int(os.getenv("TTS_AUDIO_CACHE_MAX_ENTRIES", "200"))
    max_bytes: int = int(os.getenv("TTS_AUDIO_CACHE_MAX_BYTES", str(100 * 1024 * 1024)))  # 100 MB
    max_concurrent_requests: int = int(os.getenv("TTS_MAX_CONCURRENT_REQUESTS", "4"))


class TTSUpstreamError(Exception): ...
class TTSUpstreamTimeoutError(Exception): ...


class TTSService:
    def __init__(self, config: TTSConfig):
        self._config = config
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_bytes = 0
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)

    async def get_or_generate(self, session_id: str, message_id: str, text: str) -> bytes:
        key = (session_id, message_id)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        async with self._locks[key]:
            if key in self._cache:  # another request may have just finished
                self._cache.move_to_end(key)
                return self._cache[key]

            async with self._semaphore:  # bounds concurrent upstream calls, independent of the cache
                try:
                    audio = await synthesize_speech(text)
                except asyncio.TimeoutError:
                    raise TTSUpstreamTimeoutError()
                except Exception as e:
                    raise TTSUpstreamError() from e

            self._cache_put(key, audio)
            return audio

    def _cache_put(self, key: tuple[str, str], audio: bytes) -> None:
        self._cache[key] = audio
        self._cache.move_to_end(key)
        self._cache_bytes += len(audio)
        while self._cache and (
            len(self._cache) > self._config.max_entries
            or self._cache_bytes > self._config.max_bytes
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted)
```

`TTSService` does no text preparation or length checking itself — that's decided at `/chat`
time (§4.2, §4.3), so it only ever receives already-validated text and is purely about
caching/locking/calling the API.

Notes on the remaining design choices:

- **Cache is bounded by both entry count and total bytes**, not entry count alone. Audio
  clips vary a lot in size with response length, and an entry-count-only limit doesn't
  actually bound memory — a handful of long responses could still blow past a reasonable
  budget. The byte ceiling (`TTS_AUDIO_CACHE_MAX_BYTES`) is the one that actually protects
  memory.
- **A single shared cache/lock/semaphore across all sessions, not one per session** —
  deliberate. A per-session cache would need its own bound multiplied by however many
  sessions exist concurrently; a single global cache with a global byte ceiling bounds total
  memory directly, regardless of session count, which is the property that actually matters.
  This is independent of, not redundant with, `session_isolation.md`'s own
  `SESSION_STORE_MAX_SESSIONS` bound — that bounds session *count*; this bounds audio cache
  *bytes* — both are needed.
- **In-flight lock closes the double-billing race**: two concurrent requests for the same
  uncached `(session_id, message_id)` (double-click, two tabs on the same session) both hit
  the lock; the second one's re-check after acquiring it finds the first one's result
  already cached and never calls `synthesize_speech` a second time.
- **A failed synthesis never poisons the cache**: `_cache_put` is only called after
  `synthesize_speech` succeeds, so a raised exception leaves nothing cached and a retry
  naturally re-attempts.
- **Semaphore bounds concurrent upstream calls independent of the per-key lock** — the lock
  prevents duplicate work on the *same* message; the semaphore prevents an overall burst
  (many different messages, possibly across many sessions, requested at once) from hammering
  OpenAI's own rate limits.
- **Single-process only.** This lock/cache/semaphore design coordinates within one Python
  process. The app's `Procfile` runs a single `uvicorn` process today — but if this is ever
  run with multiple workers, each gets its own independent cache/locks/semaphore, and
  duplicate synthesis across workers becomes possible again despite this design. State this
  constraint explicitly if multi-worker deployment is ever considered.

### 4.5 Response brevity in audio mode

When `audio_mode=true` is submitted with `/chat` (§3.1), the `/chat` handler builds the
system prompt as a structured list, appending an addendum rather than string-concatenating
onto `SYSTEM_PROMPT` in the route:

```python
system_instructions = [SYSTEM_PROMPT]
if audio_mode:
    system_instructions.append(AUDIO_MODE_ADDENDUM)
# join deliberately at the call site, not implicitly — keeps this legible if
# prepare_messages_with_sources's system-message handling ever changes
system_prompt = " ".join(system_instructions)

AUDIO_MODE_ADDENDUM = (
    "The user has audio playback enabled and will likely listen to this response rather "
    "than read it. Favor a succinct answer, mindful that it will be listened to rather "
    "than skimmed — but don't sacrifice completeness or clarity for brevity's sake. A few "
    "well-formed paragraphs are better than one terse one if the question genuinely calls "
    "for it. Keep any quotation brief enough to fit naturally within the response."
)
```

**Resolved (§10, previously open question 6): the audio version always matches the
on-screen text verbatim — there is no separate, shorter rendering generated just for TTS.**
`AUDIO_MODE_ADDENDUM` biases the *one* generation both are drawn from, rather than being a
second pass. The wording above deliberately avoids a hard numeric target ("one to three
paragraphs," in an earlier draft) in favor of qualitative guidance — succinct, not
truncated — since a strict paragraph cap risks the model dropping real content just to hit
the number, which cuts against the "matches on-screen verbatim" requirement: if audio mode
regularly produced worse answers than non-audio mode, the two would only be "verbatim
identical" in a technically-true, practically-hollow sense.

This addendum is about response *length*, not formatting — §4.2's finding means there's no
formatting-avoidance instruction anywhere in this spec (an earlier draft added one to the
base `SYSTEM_PROMPT`; removed once testing showed the model it was hedging against already
handles formatting fine — see §4.2). Length still matters independent of that: even
cleanly-read formatting doesn't change how long a response takes to listen through, so
keeping audio-mode replies mindful of length remains a real, separate goal — just not an
aggressively enforced one.

As a backstop — prompt instructions are advisory, not guaranteed — also pass a higher-than-
default but still bounded `max_tokens` to `get_chat_response_with_history` when
`audio_mode` is true, via `CHAT_GPT_MAX_TOKENS_AUDIO` (§6). Given the softer, "not overly
succinct" guidance above, this should be set generously — a genuine backstop against a
runaway response, not a de facto paragraph cap enforced through the back door. Treat this
as a backstop, not the primary brevity mechanism, for a concrete reason: `max_tokens` is a
*token* ceiling on chat completion output, while `TTS_MAX_INPUT_CHARS` (§4.2) is a
*character* ceiling on TTS input — tokens and characters don't convert reliably, so tuning
one doesn't let you reason precisely about the other. Don't treat "set
`CHAT_GPT_MAX_TOKENS_AUDIO` low enough" as a substitute for §4.2's own limit — they're
independent controls on different stages.

If `max_tokens` does bind (the completion's `finish_reason` comes back as `"length"`, not
`"stop"`), don't silently present the truncated text as a complete answer on screen *or* in
audio: trim to the last sentence boundary and mark the response as shortened — this affects
the on-screen rendering, not just TTS, since a truncated chat-completion output is misleading
either way it's presented. **Shipped**: `get_chat_response_with_history` returns a
`ChatCompletionResult(content, finish_reason)` rather than a bare string, so callers no longer
have to discard `finish_reason` to use this function — `/chat` reads it directly and applies
the sentence-boundary trim described above when it's `"length"`.

This reduces cost on **both** legs of a turn: fewer OpenAI chat-completion output tokens
(existing cost, incidental benefit here) and fewer TTS output audio tokens at $12/1M (§2)
— a shorter response is cheaper to generate and cheaper to synthesize.

### 4.6 Convention: named constants, not inline magic values

Applies throughout this spec's implementation, backend and frontend alike: a fixed
value — whether it's env-var-backed configuration (`TTS_MODEL`, `TTS_MAX_INPUT_CHARS`,
etc.) or a value with no reason to be externally configurable (an HTTP error-detail
string, a regex, a CSS class name, a live-region message, an `itsdangerous` salt) — gets a
clearly-named constant at the top of the file it's used in, not a literal embedded inline
where it's used. Two reasons, not just tidiness:

- **A reader scanning the top of a file sees every tunable/fixed value at a glance**,
  rather than having to read the whole file to find out what's hardcoded where.
- **A value used in more than one place (§4.3's error strings, echoed in the response-
  contract table; a live-region message set in one branch and checked in a test) has
  exactly one definition** — editing the constant changes every use, instead of hunting
  down copies that can silently drift out of sync.

This doesn't mean *every* value needs to be independently configurable via environment
variable — `TTS_ERROR_MESSAGE_NOT_FOUND` (§4.3) has no reason to be an env var, but it
still gets a named constant rather than a bare string literal in the `raise` call. Env-var-
backed values (`os.getenv("X", DEFAULT)`) already satisfy this by construction, as long as
the assignment itself lives at module scope, at the top of the file — not inside a function
body where it'd be re-evaluated or harder to find.

On the frontend, the same applies to `script.js`'s status-message strings ("Generating
audio…", "Playing audio", etc., §5.2) and any CSS class names introduced for this feature
(§5.3) — define them once, near the top of the file, and reference them everywhere they're
used rather than retyping the string at each call site.

## 5. Frontend design

### 5.1 Template changes (`bot_message.html`)

```html
{% if message.tts_available %}
<button class="btn btn-sm btn-outline-secondary tts-toggle" type="button"
        data-message-id="{{ message_id }}"
        aria-label="Read this response aloud">
    🔊 Listen
</button>
{% else %}
<button class="btn btn-sm btn-outline-secondary" type="button" disabled
        title="This response is too long to read aloud"
        aria-label="Audio unavailable: this response is too long to read aloud">
    🔊 Listen
</button>
{% endif %}
<div class="visually-hidden" role="status" aria-live="polite" id="tts-status-{{ message_id }}"></div>
<audio id="tts-audio-{{ message_id }}"></audio>
```

`message.tts_available` (§4.4) is known at render time — computed once in `/chat`, not
discovered by clicking — so the disabled state above never requires a round trip to
discover. The `<audio>` element starts with no `src` — nothing is fetched until the button
is clicked and its `POST` response is turned into a blob URL (§5.2).

### 5.2 `script.js` changes

**Explicit per-message playback state**, not just "is something playing": each Listen
button/audio pair moves through `idle → loading → playing → idle`, with `error` reachable
from `loading` or `playing`. Track state per `message_id` (a small `Map`, not a single
global "currently playing" variable) so a stale async response can't clobber a newer
click's state — see the stale-response guard below.

- **Click while `idle`**: state → `loading`, button shows "Generating audio…", live region
  updates. Create an `AbortController`; store it alongside a request token (e.g. an
  incrementing counter per `message_id`) so a later click on the same message can
  invalidate this in-flight request's effect on the UI.
  - `POST /api/messages/{message_id}/speech` with the controller's `signal`.
  - Non-2xx response: parse the JSON `detail` (§4.3) and show a specific message —
    `413` → "This response is too long to read aloud in one piece." (though the disabled
    button in §5.1 should make this rare in practice); `429` → "Please wait a moment before
    trying again."; otherwise → "Audio unavailable." State → `error` (then back to `idle`
    after a moment, or on next click).
  - 2xx response: `response.blob()` → `URL.createObjectURL(blob)` → assign to the
    `<audio>` element's `src` → `await audio.play()`.
    - `audio.play()` returns a promise that can reject independently of any `onerror`
      handler (some browsers reject it on rapid interaction, or for autoplay-policy
      reasons per §3's caveat about the gesture "expiring" during the wait). Wrap in
      `try/catch`, not just `onerror` — a bare `onerror`-only implementation misses this.
  - Before acting on the response (blob or error), check the request token is still the
    latest for this `message_id` — if the user clicked Stop, or clicked Listen again,
    while this request was in flight, discard the now-stale result instead of overwriting
    newer UI state.
- **Click while `loading`**: treat as Cancel — call the stored `AbortController.abort()`,
  state → `idle`. Note: aborting the *browser's* request doesn't necessarily cancel
  server-side work already handed off to OpenAI — the in-flight `TTSService` call (§4.4)
  keeps running and, on success, still populates the cache (which is fine — a future
  request benefits from it) but this abort does **not** guarantee the OpenAI call itself
  was avoided or its cost avoided. State that limitation rather than implying Cancel is
  free.
- **Click while `playing`**: `.pause()`, `currentTime = 0`, state → `idle`.
- **`audio.onended`**: state → `idle` (natural completion, not a manual stop).
- Starting playback on a different message: if another message is currently `playing` or
  `loading`, force it to `idle` (pause/abort as appropriate) before starting the new one —
  only one message plays at a time.
- **Object URL cleanup**: `URL.revokeObjectURL(...)` when a message's audio is superseded
  by another message starting, on `onended`, and on page unload — avoids leaking blob
  memory across a long session.

**HTMX-swap safety, confirmed rather than assumed:** both `chat.html`'s form
(`hx-swap="beforeend"`) and `script.js`'s own DOM handling only ever *append* new bot
messages — no existing message's markup is replaced or re-rendered afterward. So per-message
state tracked by `message_id` can't end up pointing at a detached node; checking the actual
swap strategy in this codebase confirms this is a non-issue here, not something needing a
defensive workaround.

### 5.3 Styling (`styles.css`)

Reuse the existing `.btn-outline-secondary` styling already applied to the sources toggle
button, for visual consistency — no new color variables needed. The `disabled` state (§5.1)
gets whatever Bootstrap's default disabled-button treatment already provides.

## 6. Configuration

| Env var | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | Already required by the app; reused for speech synthesis | — |
| `TTS_MODEL` | Speech model | `gpt-4o-mini-tts` |
| `TTS_VOICE` | Voice | `cedar` |
| `TTS_SPEED` | Playback speed passed to the API (`0.25`–`4.0` per OpenAI's documented range) | `1.0` |
| `TTS_TIMEOUT_SECONDS` | Per-request timeout on the OpenAI speech call (§4.1) | `30` |
| `TTS_MAX_INPUT_CHARS` | Safety-margin character budget, checked once at `/chat` time against the raw response (§4.2) | `3800` |
| `TTS_AUDIO_CACHE_MAX_ENTRIES` | Max cached audio clips before eviction (§4.4) | `200` |
| `TTS_AUDIO_CACHE_MAX_BYTES` | Max total cached audio bytes before eviction (§4.4) | `104857600` (100 MB) |
| `TTS_MAX_CONCURRENT_REQUESTS` | Semaphore bound on concurrent upstream OpenAI calls (§4.4) | `4` |
| `TTS_RATE_LIMIT` | Per-session rate limit on the speech endpoint (§8), e.g. `"10/minute"` | `10/minute` |
| `CHAT_GPT_MAX_TOKENS_AUDIO` | Chat completion `max_tokens` ceiling used instead of `CHAT_GPT_MAX_TOKENS` when `audio_mode=true` (§4.5) | e.g. `800` — a genuine backstop, not a de facto length target; kept generous per §4.5's "succinct, not overly so" guidance |

Session-related configuration (`SESSION_SECRET_KEY`, `SESSION_TTL_DAYS`, etc.) is defined in
[`session_isolation.md`](session_isolation.md) §11, not duplicated here.

Output format is deliberately **not** configurable via env var — it's fixed to `mp3` at the
application level (§2, §4.3). A configurable format alongside a hardcoded
`media_type="audio/mpeg"` response header would be a correctness bug — fixing the format
avoids that mismatch entirely.

Add all of these to `.env.example`. Validated at startup, not lazily (invalid `TTS_SPEED`
outside `0.25`–`4.0`, unknown voice/model, etc. should fail startup with a clear error, the
same posture `session_isolation.md` takes for `SESSION_SECRET_KEY`).

## 7. Known limitations

- **No persistence across restarts.** Session state (`session_isolation.md`) and
  `TTSService`'s internal cache/locks are all in-memory — a restart means old "Listen"
  buttons 404. Acceptable given the app's existing no-persistence design.
- **Single-process only** (§4.4) — dedup and rate limiting are per-worker, not
  cluster-wide, if this ever runs with multiple processes.
- **Markdown-handling behavior is verified only against a small smoke-test corpus** (§4.2)
  — seven short, largely independent examples of the highest-risk constructs, run once
  against `gpt-4o-mini-tts`/`cedar` specifically. Not exhaustive: deeply nested structures,
  long tables, multiple consecutive links, and this app's own archaic-English quoted
  material haven't been separately checked. §9's audio-quality corpus is the ongoing
  mechanism for catching a regression or an uncovered case, not a one-time gate that's
  already been fully passed.
- **A different voice or model would need re-verification.** The finding in §4.2 is
  specific to `gpt-4o-mini-tts`/`cedar` — nothing here implies it generalizes to `tts-1`/
  `tts-1-hd` (which are not LLM-based, per §2) or to a different voice/model chosen later
  via `TTS_VOICE`/`TTS_MODEL` (§6). Re-run §4.2's smoke test if either changes.
- **Oversized responses are rejected, not chunked** (§4.2) — a user sees a disabled Listen
  button rather than a multi-clip playback experience. Accepted for v1; chunking is a real
  fast-follow if this fires often in practice (§10).
- **Audio-mode brevity (§4.5) is deliberately soft, not a hard cap** — qualitative
  guidance ("succinct, not overly so"), backed only by a generous `max_tokens` backstop, not
  a strict paragraph count. This is intentional (§10, resolved question 5/on-screen
  divergence) but means audio-mode responses won't always be dramatically shorter than
  normal ones — if the question genuinely calls for depth, the response can still be long.
  If `max_tokens` does bind, the `finish_reason`-aware trim in §4.5 softens but doesn't
  eliminate the awkwardness of a shortened reply.
- **Cost is unbounded per unique message, bounded per unit time** — §8's rate limit and
  §4.4's concurrency semaphore bound *abuse rate* and *burst load*; neither bounds total
  legitimate-use spend across a long session. Flag if usage patterns suggest a spend cap is
  needed.
- **Client-side abort doesn't guarantee upstream cost avoidance** (§5.2) — canceling a
  request in the browser doesn't reliably cancel work already handed to OpenAI.

## 8. Security and abuse

Session isolation — who can see or act on whose messages — is resolved by
[`session_isolation.md`](session_isolation.md) (Option B, decided): `chat_history` and
`messages_by_id` are scoped per session (§4.4 above), so a `message_id` from one session
can't be read or synthesized by another. This section covers what's specific to the TTS
endpoint on top of that.

`POST /api/messages/{message_id}/speech` is this app's **first endpoint that triggers a
paid, per-request external API call**. `POST` (§4.3) already closes the *unintentional*-
trigger surface (crawlers, prefetchers, link scanners). What's left is bounding legitimate
callers' *rate*, for the same denial-of-wallet reason `session_isolation.md` §6 rate-limits
`/chat`:

**Rate limiting**, per session (not per-IP — a session is the real identity boundary here
now that one exists; per-IP would both under- and over-count relative to actual users behind
shared connections): via `slowapi`, configurable through `TTS_RATE_LIMIT` (§6, default
`10/minute`). Defined canonically in [`session_isolation.md`](session_isolation.md) §6
alongside `/chat`'s own limit, since both routes share the same rate-limiting
infrastructure — this section just notes the value specific to this endpoint.

This bounds *rate*; it doesn't bound total legitimate-use spend across a long session (§7)
— that's a separate, still-open question (§10).

## 9. Testing plan

Follow the existing `monkeypatch`/mock-based patterns in
[`tests/test_chat_gpt_client.py`](../../tests/test_chat_gpt_client.py) and
[`tests/test_main.py`](../../tests/test_main.py). `TTSService` (§4.4) being a standalone
object, not bare module dicts, makes it directly unit-testable rather than only reachable
through the FastAPI route — prefer that where possible below.

**Backend:**

1. `tests/test_tts_client.py` — patch `app.tts_client.client`, assert `synthesize_speech()`
   returns the mocked bytes on success and raises on a simulated API error.
2. `tests/test_tts_service.py` (new, testing `TTSService` directly, no FastAPI needed):
   - Cache hit (same `(session_id, message_id)`) returns without calling
     `synthesize_speech` again.
   - Two different sessions requesting the *same* `message_id` value (collision on the
     message-id half of the key alone) are treated as distinct cache entries — confirms the
     composite key, not just the presence of a cache.
   - **Concurrency**: fire two `get_or_generate()` calls for the same uncached key
     concurrently (`asyncio.gather`, with a mocked `synthesize_speech` that `await
     asyncio.sleep(...)` before returning so both calls are genuinely in-flight together)
     and assert `synthesize_speech` was called exactly once.
   - A failed `synthesize_speech` call leaves nothing cached, and a subsequent call
     retries (not poisoned).
   - Eviction: populate past `max_entries` and separately past `max_bytes`, assert the
     oldest entries are evicted and the newest survive, in both cases independently.
3. `tests/test_main.py` additions:
   - POST `/chat` with a mocked long chat response (longer than `TTS_MAX_INPUT_CHARS`) and
     assert the resulting `Message` has `tts_available=False`; assert the *rendered HTML*
     shows the disabled-Listen-button branch (§5.1), not the enabled one — this is the
     eager-precompute behavior (§4.2/§4.4), not just the route-level 413 in the next bullet.
     POST with a short response and assert `tts_available=True` and the enabled-button
     branch.
   - Monkeypatch the TTS path the same way `get_chat_response_with_history` is patched
     today. POST `/chat`, extract `message_id` from rendered HTML, then `POST
     /api/messages/{message_id}/speech` and assert `200`, `content-type: audio/mpeg`, body
     equals the mocked bytes, and that `synthesize_speech` was called with the message's
     `content` unmodified (no transformation applied — confirms §4.2's "sent as-is" design).
   - `POST` to an unknown `message_id` → `404`; `POST` to a `message_id` that exists but
     under a *different* session's cookie → also `404`, and asserted to be
     response-indistinguishable from the unknown case (`session_isolation.md` §5).
   - `POST` for a message with `tts_available=False` → `413` — and assert `synthesize_speech`
     was never called for it (the route short-circuits before touching `TTSService` at all).
   - Rate limit: assert the `(N+1)`th request within the configured window from the same
     session gets `429`.
   - Audio mode → prompt/token wiring: `POST /chat` with `audio_mode=true` and assert (via
     monkeypatched call args, same approach as `mock_services`) the system prompt includes
     `AUDIO_MODE_ADDENDUM` and `max_tokens` equals `CHAT_GPT_MAX_TOKENS_AUDIO`; omitting
     `audio_mode` leaves both unchanged. Separately assert that omitting the checkbox
     entirely (not sending the field at all) is treated identically to an explicit
     unchecked state — the HTML-forms-omit-unchecked-checkboxes case (§3.1).
   - Config validation: invalid `TTS_SPEED`/voice/model at startup raises a clear
     configuration error rather than either crashing obscurely or silently proceeding.

**Frontend** (manual/browser check is not sufficient alone for this much state — add
scripted DOM tests where the project's tooling allows, otherwise treat the list below as
the manual test script, not a shortcut):

- `idle → loading → playing → idle` (natural end) happy path.
- A message rendered with a disabled Listen button (§5.1) is genuinely inert — no click
  handler fires, no request is sent.
- Cancel while `loading` (before the response arrives).
- Stop while `playing`.
- Starting a second message's audio stops/resets the first.
- A stale response (superseded by a newer click) doesn't overwrite the newer state.
- Rejected `play()` promise is caught and surfaces an error state, not a stuck spinner.
- `404`/`413`/`429`/`502` each produce their own distinct live-region message.
- Object URLs are revoked (no unbounded growth over many plays in one session).
- `aria-label` and live-region content are correct in every state above.
- Keyboard activation and visible focus on the Listen/Stop button.
- Checkbox restoration from `localStorage` on page load.

**Audio quality evaluation.** A small fixed corpus, run through actual synthesis and
listened to — now a regression check confirming §4.2's finding continues to hold, not the
primary risk-mitigation mechanism it was designed as originally:

1. Ordinary prose (no formatting).
2. A heading plus both an ordered and an unordered list.
3. A Markdown link and a bare URL.
4. A citation-heavy RAG answer with an attributed quote.
5. A short fenced code block and an inline code span.
6. Pipe-table syntax.
7. **Archaic/Early Modern English text**, drawn from this app's actual corpus — George
   Fox's own writing style (`thee`/`thou`, period spelling, biblical citation style) is the
   real out-of-distribution case for this app, and wasn't part of §4.2's smoke test; pull a
   passage directly from `texts/`.
8. Numbers, dates, and abbreviations as they actually appear in retrieved passages.
9. A verbatim quotation long enough to approach `TTS_MAX_INPUT_CHARS`.

## 10. Open questions for the user

1. **Auto-read-on-arrival mode — resolved, implemented.** Rather than a separate
   always-on toggle, this reuses the existing audio-mode checkbox (§3.1) as the auto-play
   signal (per this section's original reasoning: opting into shorter replies is itself a
   stronger signal of listening intent than a second control would be). §3 documents the
   mechanics. Not addressed: interrupting mid-playback if the user starts typing a new
   message while one is still playing — today a second `/chat` response arriving mid-playback
   will stop the current one (only one message plays at a time, per the existing rule), but
   nothing stops playback the moment typing starts. Still a reasonable fast-follow if this
   proves annoying in practice.
2. **Streaming playback**, and **chunked playback for oversized responses** (§4.2's `413`
   rejection vs. splitting into multiple sequential clips) — both are real fast-follow
   candidates if either "the wait is noticeable" or "responses regularly exceed the input
   limit" turn out to be true in practice. Neither is built here.
3. **Cost guardrails beyond rate limiting.** §8 bounds abuse *rate*; it doesn't bound total
   legitimate-use spend (§7). Is a per-session play-count or token budget wanted, or is
   rate limiting sufficient for now?
4. **Voice/speed as a user-facing setting**, vs. the fixed server-side `TTS_VOICE`/
   `TTS_SPEED` env vars (§6) — per-user override wanted, or a single site-wide voice
   acceptable? Note §7: changing the voice/model would call for re-running §4.2's smoke
   test before assuming the same markdown-handling behavior still applies.
5. **Brevity vs. verbatim-quote fidelity — resolved by §4.2 plus the §4.5 wording
   refinement.** Earlier drafts worried a text-transformation step (since removed) might
   corrupt a verbatim quote — with no transformation at all, that risk is gone, and the
   audio/on-screen text are always identical in content. The remaining generation-time
   angle (audio mode pressuring the model to shorten a quote) is now addressed directly in
   `AUDIO_MODE_ADDENDUM` itself — "don't sacrifice completeness or clarity for brevity's
   sake" applies to quotes the same as everything else. No longer treated as open.
6. **Exact AI-voice disclosure wording** (§2, §3) — verify against OpenAI's current usage
   policy at implementation time rather than treating the placeholder copy here as final.

Three questions from earlier drafts are now resolved, not just deferred:

- **Session isolation** — [`session_isolation.md`](session_isolation.md) is decided
  (Option B), and §4.3/§4.4/§8 above integrate with it directly.
- **On-screen vs. spoken content diverging** — resolved: the audio version always matches
  the on-screen text verbatim (§4.5) — there is no separate TTS-only rendering. Audio mode
  biases the single shared generation toward succinctness without sacrificing completeness,
  rather than producing a second, shorter version.
- **The base-`SYSTEM_PROMPT` formatting tradeoff** — moot. §4.2 removed the
  formatting-avoidance instruction entirely once testing showed the model it was hedging
  against already handles formatting fine; there's no on-screen-style tradeoff left to
  confirm.

## 11. Dependencies to add

- **`slowapi`** (or an equivalent lightweight FastAPI rate-limiting library) for §8's
  per-endpoint rate limit — shared with `/chat`'s rate limit, defined in
  [`session_isolation.md`](session_isolation.md) §12.

That's the only new dependency this spec needs. `openai` is already a runtime dependency; its
async speech-response API surface is confirmed against the pinned version to match §4.1's
`with_streaming_response`/`response.read()` pattern.

**No markdown-handling dependency is needed.** Earlier drafts evaluated and, at different
points, both rejected (`strip-markdown` and other small PyPI packages: unmaintained;
`langchain_community`: wrong tool, oversized for the job) and adopted (`markdown-it-py`) a
markdown-processing dependency for TTS input preparation. §4.2's empirical finding
supersedes that whole line of investigation — no text transformation happens at all, so no
markdown-parsing library, of any kind, is needed for this feature.

## 12. Acceptance criteria

1. A user can play and stop any bot response using keyboard or pointer controls, with
   correct `aria-label`/live-region announcements in every state, including a genuinely
   inert disabled state for responses too long for TTS (§5.1).
2. Only one response plays at a time.
3. A given `(session, message)` pair is synthesized at most once under concurrent requests,
   within this app's single-process deployment model (§4.4) — multi-process is an
   explicitly stated non-goal, not silently assumed to work.
4. Billed generation only ever happens via an intentional `POST`, never a `GET` or any
   passive/automated request (§4.3).
5. Input length is checked once, at `/chat` time, against the unmodified response — not
   discovered lazily by clicking Listen, and no text transformation is applied before
   sending to TTS (§4.2). Oversized input is rejected with a clear message, shown before
   the click.
6. Cache memory use is bounded by both entry count and total bytes, globally, not per
   session.
7. Users see a visible (not merely programmatic) AI-voice disclosure.
8. Errors map to specific, non-leaking status codes and messages (§4.3), not a single
   blanket failure state.
9. One user cannot access another user's message audio — resolved via
   [`session_isolation.md`](session_isolation.md) (Option B), not an accepted open risk.
10. The feature works in current target versions of Chrome, Firefox, Edge, Safari, and
    mobile Safari — specifically including the play()-after-delay autoplay-policy check
    flagged in §3.
