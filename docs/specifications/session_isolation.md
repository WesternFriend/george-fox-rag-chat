# Session Isolation Specification

Status: **decided — Option B (real session scoping)**, confirmed 2026-07-25. Originally
spun out from a peer review of [`text_to_speech.md`](text_to_speech.md) §8, which flagged
that the app had no per-session state boundary; the user has since confirmed the app is
intended for multi-tenant deployment, which settles the Option A/B question this spec was
written to resolve.

## 1. Problem statement

`app/main.py` holds chat state in a single process-wide, global variable:

```python
chat_history: List[Message] = []
```

This is **not** scoped per browser, per cookie, or per user — it's one list, shared by
every request the process handles, for the life of the process. Concretely, today:

- `GET /` renders `chat.html` with the entire global `chat_history` — a second browser
  tab, or a second person's browser, sees the same conversation.
- `POST /chat` appends to and reads from that same global list regardless of caller.
- `GET /api/chat_history` returns the entire global list as JSON to any caller, no auth.
- `POST /api/clear_history` clears it for everyone, for any caller, no auth.

## 2. Decision and the confirmed requirements behind it

The user confirmed:

1. **Multi-tenant, with resumable sessions.** The app is intended for multiple concurrent
   users, each with their own private conversation. A returning visitor should be able to
   resume their own session — not just get a fresh one every visit — but sessions must
   never bleed across users/browsers.
2. **Basic rate limiting**, motivated specifically by wanting to avoid a
   denial-of-wallet attack (unbounded OpenAI spend triggered by an unauthenticated
   caller). An invitation-only gate is a possible future addition, but not now — rate
   limiting is the near-term mitigation.
3. **Session eviction**: TTL-based expiry plus a maximum retained message count per
   session (truncate oldest messages beyond a cap), deferring to this spec's recommendation
   for exact numbers.
4. **Threat model**: the user accepts browser-level cookie security as a working
   assumption (cross-site cookie theft treated as difficult) — combined with TTL expiry and
   message truncation, this is judged as an acceptable degree of privacy for this app. This
   spec still applies standard, free cookie hardening (`HttpOnly`/`Secure`/`SameSite`, §4)
   regardless — those defend against a different threat (XSS/CSRF) than the one being
   explicitly accepted here, so applying them isn't redundant with that acceptance.

This settles the Option A vs. B question the original draft of this spec left open: **Option
B (real session scoping)** is adopted. Option A (accept the global model, document it) is
no longer under consideration, given confirmed requirement 1.

## 3. Session identity: a minimal, opaque cookie — not Starlette's `SessionMiddleware`

**Correction from an earlier draft of this spec:** Starlette's built-in `SessionMiddleware`
was proposed there as an off-the-shelf option. On closer inspection it's the wrong tool
here: `SessionMiddleware` signs and stores the **entire session payload inside the cookie
itself** — there's no server-side store in its default design. For this app that would mean
either putting all of `chat_history` inside the cookie (unworkable — cookies are capped
around 4KB, and the browser would resend the entire conversation on every request) or
storing just an id *inside* that mechanism, which works but pulls in a session-dict
abstraction this app doesn't need for a single opaque value.

Instead: sign a **single random session id** directly with `itsdangerous`
(`URLSafeTimedSerializer`), and keep everything else server-side (§4). The cookie carries
no personal data — just an opaque, unguessable identifier — which is also the right shape
for the GDPR framing in §7.

Constants at module scope, not inline literals — see the convention note in §14:

```python
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import secrets

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "gfrag_session")
SESSION_SECRET_KEY = os.environ["SESSION_SECRET_KEY"]  # required, no default — fail startup if unset
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_DAYS", "30")) * 86400
SESSION_COOKIE_SALT = "session-cookie"  # itsdangerous salt — not a secret itself, but named
                                         # rather than inlined so it's obviously the same
                                         # value used everywhere a cookie is signed or read

_serializer = URLSafeTimedSerializer(SESSION_SECRET_KEY, salt=SESSION_COOKIE_SALT)


def read_session_id(request: Request) -> str | None:
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if raw is None:
        return None
    try:
        return _serializer.loads(raw, max_age=SESSION_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None  # tampered, unsigned, or expired — treated as "no session", not an error


def issue_session_id() -> str:
    return secrets.token_urlsafe(32)


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _serializer.dumps(session_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,       # requires HTTPS in production; see note below for local dev
        samesite="lax",
        path="/",
    )
```

**Sliding expiration**: every request that resolves a valid session re-sets the cookie
(same call as above), refreshing `max_age` — an actively-used session's cookie never
expires; only genuinely idle sessions age out at `SESSION_TTL_DAYS`.

**Local dev note**: `secure=True` means the cookie won't be set over plain HTTP — `uvicorn
--reload` on `localhost` typically needs either HTTPS locally or a dev-only override.
Don't silently disable `secure` in production code paths for this — gate it behind
`ENVIRONMENT=development` or similar, explicit and impossible to leave on by accident.

**No session ⇒ mint one.** Any request with no valid session cookie (missing, expired, or
failed signature verification) gets a freshly issued `session_id` and a `Set-Cookie` on the
response — this covers first-time visitors and the tampered/expired cases identically,
deliberately: there's no user-visible distinction between "you never had a session" and
"your session expired," which avoids leaking anything about *why* a given cookie didn't
validate.

## 4. Server-side session store

A `SessionStore`, mirroring the `TTSService`/`RAGService` shape already used elsewhere in
this app's design:

```python
# SESSION_TTL_SECONDS is the same constant introduced in §3 — shown again here only
# because these are separate illustrative snippets; in the real implementation this and
# the two constants below live in one shared module (e.g. app/session_config.py) and are
# imported wherever needed, not redefined per file.
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_DAYS", "30")) * 86400
SESSION_MAX_MESSAGES = int(os.getenv("SESSION_MAX_MESSAGES", "100"))
SESSION_STORE_MAX_SESSIONS = int(os.getenv("SESSION_STORE_MAX_SESSIONS", "1000"))


@dataclass
class SessionConfig:
    ttl_seconds: int = SESSION_TTL_SECONDS
    max_messages: int = SESSION_MAX_MESSAGES
    max_sessions: int = SESSION_STORE_MAX_SESSIONS


@dataclass
class SessionState:
    chat_history: list[Message] = field(default_factory=list)
    messages_by_id: dict[str, Message] = field(default_factory=dict)  # moved here from being global — see text_to_speech.md §4.4
    last_active: float = 0.0  # a monotonic/epoch timestamp, stamped by the caller


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
        state = SessionState(last_active=now)
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

    def sweep_expired(self, now: float) -> None:
        """Call periodically (§4.1) — proactively remove idle-expired sessions rather than
        relying only on eviction-at-capacity, which only fires once the store is full."""
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_active > self._config.ttl_seconds
        ]
        for sid in expired:
            del self._sessions[sid]

    def _evict_if_over_capacity(self) -> None:
        while len(self._sessions) > self._config.max_sessions:
            self._sessions.popitem(last=False)  # evict least-recently-active
```

Two bounds, addressing two different risks:

- **`SESSION_MAX_MESSAGES`** (per-session message cap, truncating oldest) — bounds memory
  *per session* and gives a privacy benefit independent of TTL: even an actively-used,
  long-lived session doesn't retain unlimited history.
- **`SESSION_STORE_MAX_SESSIONS`** (total concurrent sessions, LRU-evicted) — bounds memory
  *across* sessions. This addresses a resource-exhaustion angle the user's "denial-of-wallet"
  framing doesn't automatically cover: an attacker who never returns a session cookie
  forces a new `SessionState` on every request, and without this cap that's an unbounded
  memory-growth vector distinct from (but related to) the OpenAI-cost concern §6 addresses.

### 4.1 Periodic sweep

LRU eviction-at-capacity only reclaims memory once the store is full; it doesn't
proactively free idle sessions below that ceiling. Run `sweep_expired()` on an interval via
FastAPI's lifespan (`asyncio` background task started at app startup, cancelled at
shutdown) — e.g. every 15–60 minutes; exact interval isn't sensitive, since the cost of
running it a bit more or less often is small relative to `SESSION_TTL_DAYS`.

## 5. Routes affected

- `GET /` — reads or mints a session id (§3), renders that session's `chat_history` (via
  `SessionStore.get_or_create`).
- `POST /chat` — appends to the caller's session only, via `SessionStore.append_message`.
- `GET /api/chat_history` — returns only the caller's session's history, not the global
  store.
- `POST /api/clear_history` — clears only the caller's session's `chat_history`/
  `messages_by_id`. Keeps the same `session_id`/cookie rather than also resetting identity
  — "clear my conversation" and "forget who I am" are different user intents, and
  conflating them would undermine the "resume later" requirement (§2, point 1) for no
  clear benefit.
- [`text_to_speech.md`](text_to_speech.md)'s `POST /api/messages/{message_id}/speech` —
  looks up the message via the caller's session's `messages_by_id`, not a global dict.
  A `message_id` valid in a *different* session, or a `message_id` that never existed,
  both resolve to the same `404` — indistinguishable on purpose, so a response never
  confirms to a caller that a given `message_id` exists but belongs to someone else.

## 6. Rate limiting

Resolves the "if Option A" question from the original draft — moot now that Option B is
decided, but the *rate limiting itself* is still wanted independently (§2, point 2), keyed
by `session_id` now that one reliably exists (more accurate than per-IP, since IP can be
shared across unrelated users behind NAT/a proxy — session id is the actual identity
boundary this spec establishes). Fall back to per-IP only for the rare case of a request
with no session context yet.

Using `slowapi` (already proposed in `text_to_speech.md` §11 for its own endpoint;
defining shared usage here since scope has grown beyond TTS alone):

| Route | Proposed limit | Why |
|---|---|---|
| `POST /chat` | `20/minute` per session | Each call is a billed OpenAI chat completion — this app's core denial-of-wallet surface, not previously rate-limited at all. |
| `POST /api/messages/{message_id}/speech` | `10/minute` per session | As already proposed in `text_to_speech.md` §8 — kept here as the canonical definition; that spec should reference this one rather than duplicate it. |

These are starting defaults, not empirically tuned — expect to revisit once real usage
data exists, and likely to relax once/if the invitation-only gate mentioned in §2 exists,
since that changes the abuse surface substantially.

## 7. GDPR / privacy posture

*Not legal advice — a description of the engineering choices made and why they lean toward
compliance, for the user (or counsel) to confirm formally if this matters for a real
deployment.*

- The session cookie carries **only an opaque, signed random identifier** — no personal
  data, no tracking/analytics purpose, no third-party sharing. Its sole purpose is
  maintaining conversation state the user themselves generated, which is the kind of
  purpose the EU ePrivacy Directive's "strictly necessary" exemption is aimed at (cookies
  used purely to provide a service the user actively requested, as opposed to
  advertising/analytics cookies, are generally exempt from the *consent-banner*
  requirement specifically — this is a narrower question than GDPR compliance generally).
- **Storage limitation**: `SESSION_TTL_DAYS` and `SESSION_MAX_MESSAGES` (§4) both bound how
  long and how much data is retained — directly supporting the GDPR storage-limitation
  principle rather than retaining conversations indefinitely.
- **Purpose limitation**: session data is used only to maintain the conversation itself,
  never repurposed for tracking, profiling, or shared with third parties.
- **Transparency**: recommend a short, visible notice (a line in the page footer or near
  the chat input) stating that a session cookie is used to remember the conversation, for
  how long, and that clearing history is available (`POST /api/clear_history`). Simple to
  add, and closes the transparency-principle gap a purely technical design doesn't address
  on its own.
- If any future addition changes this purpose (analytics, marketing, cross-site tracking),
  that cookie would need its own consent flow — explicitly out of scope for, and not
  implied by, this spec's session-identity cookie.

## 8. Authentication is a different, unaddressed problem

Session **isolation** (this spec) means one browser's conversation doesn't bleed into
another's. It does **not** mean the session is tied to a verified identity — anyone holding
a valid session cookie (via that browser, or a copied cookie value) *is* that session, full
stop. The user's accepted threat model (§2, point 4) already treats cookie theft as
out-of-scope-in-practice; this is stated here as an explicit design boundary regardless, not
a bug: if real authentication (login, verified identity) is ever wanted, that's a separate
spec, layered on top of — not a replacement for — the isolation this one provides.

## 9. Restart behavior

Storage is in-memory only (§4), consistent with this app's existing no-persistence design —
a server restart empties the `SessionStore` entirely. A returning visitor's still-valid
cookie will then reference a `session_id` the store has never seen. `get_or_create` (§4)
already handles this correctly by construction: an unknown `session_id` just gets a fresh
`SessionState`, not an error — the user experiences this as "my history is gone," not a
broken app. Accepted, consistent with `chat_history`'s pre-existing behavior; real
persistence (Redis, a lightweight DB) would be needed to survive restarts and is
deliberately not built here — flag if that's wanted.

## 10. Testing plan

1. First visit (no cookie) → response sets a session cookie; second request with that
   cookie → same session, no new cookie needed (or a refreshed one with the same value,
   per sliding expiration).
2. Two independent cookies (simulating two browsers) never see each other's
   `chat_history`, `GET /api/chat_history` results, or TTS `message_id` lookups.
3. A tampered cookie value (flip a character) or an expired one is treated as "no
   session" (a fresh one is minted), not a 500 or other error.
4. `message_id` valid in session A returns `404` (not `403`, not the audio) when requested
   under session B's cookie.
5. Message truncation: append more than `SESSION_MAX_MESSAGES`, assert the oldest are
   dropped, the newest retained, count stays capped, and `messages_by_id` no longer
   contains the dropped messages' ids.
6. Session cap: create more than `SESSION_STORE_MAX_SESSIONS` distinct sessions, assert
   the least-recently-active is evicted, not an arbitrary or newest one.
7. TTL expiry: with a short TTL injected via config in the test, confirm a session past
   its idle window is treated as gone (fresh `SessionState` on next access) — needs a
   controllable/injectable clock rather than real `sleep()`.
8. Rate limiting: the `(N+1)`th request within a window from the same session gets `429`,
   scoped per-session (a second session's requests aren't affected by the first session
   hitting its own limit).
9. `POST /api/clear_history` empties the caller's session's history but the session id/
   cookie itself is unchanged — a subsequent message still lands in the same session.

## 11. Configuration

| Env var | Purpose | Default |
|---|---|---|
| `SESSION_SECRET_KEY` | Signs the session cookie — **required, no default**; fail startup if unset, since an unset/weak key defeats the entire signing scheme | — |
| `SESSION_COOKIE_NAME` | Cookie name | `gfrag_session` |
| `SESSION_TTL_DAYS` | Sliding idle-expiry window | `30` |
| `SESSION_MAX_MESSAGES` | Per-session retained message cap (oldest truncated) | `100` |
| `SESSION_STORE_MAX_SESSIONS` | Total concurrent sessions before LRU eviction | e.g. `1000` (tune to expected concurrency and available memory) |
| `CHAT_RATE_LIMIT` | Per-session rate limit on `POST /chat` (§6) | `20/minute` |

`TTS_RATE_LIMIT` is defined in `text_to_speech.md` §6 and reused here (§6) rather than
duplicated.

## 12. Dependencies

- **`itsdangerous`** — promote from a transitive Starlette dependency to a direct one in
  `pyproject.toml`, since this app's own code now imports and calls it directly (§3), not
  just relying on it existing incidentally.
- **`slowapi`** (already proposed in `text_to_speech.md` §11) — now used for `/chat`'s
  rate limit too (§6), not only TTS's.
- No new heavyweight framework. Starlette's `SessionMiddleware` is deliberately **not**
  used (§3) — the hand-rolled opaque-id cookie keeps the cookie payload minimal, which
  also directly supports the GDPR framing in §7 (an opaque id is easier to justify as
  "strictly necessary and minimal" than a cookie carrying an entire session payload would
  be).

## 13. Open items

- **Numeric defaults — confirmed, not just proposed.** The user accepted this spec's
  recommended starting points (`SESSION_TTL_DAYS=30`, `SESSION_MAX_MESSAGES=100`,
  `SESSION_STORE_MAX_SESSIONS=1000`, `CHAT_RATE_LIMIT=20/minute`, and `TTS_RATE_LIMIT
  =10/minute` from `text_to_speech.md` §6). These aren't empirically validated against real
  usage — they're reasonable starting points, not measured — so still expect to revisit
  once real usage data exists, but they're locked in as the implementation's starting
  values, not a placeholder awaiting sign-off.
- Whether `POST /api/clear_history` should also let a user explicitly start an entirely
  new session (new id, not just cleared history) is a small, separate UX question not
  addressed here — current design keeps the same id (§5).

## 14. Convention: named constants, not inline magic values

Same convention as [`text_to_speech.md`](text_to_speech.md) §4.6, stated here too since
this spec's own code (the cookie-signing functions in §3, `SessionConfig` in §4) is
implemented independently: every fixed value — env-var-backed (`SESSION_TTL_DAYS`,
`CHAT_RATE_LIMIT`, etc.) or not (`SESSION_COOKIE_SALT`, §3) — gets a named constant at the
top of the file it's used in, not a literal embedded where it's used. This matters
specifically for values reused in more than one place — e.g. the salt in §3 is used both
when signing a new cookie and when verifying one on every request; naming it once removes
any chance of the two call sites drifting apart. `SessionConfig` (§4) should be constructed
from named module-level constants the same way, not built from literals inline at the
construction site.
