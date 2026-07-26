# Accessibility Improvements: Audit Findings and Plan

Status: **Phases 1–3 fixed, 2026-07-26** (Phase 4 items remain — see below). This is a snapshot of
the app's current state against
[`docs/specifications/accessibility.md`](specifications/accessibility.md) — a working
punch list, not a durable design doc. Update or remove items here as they're fixed; the
guidelines they're checked against live in the linked spec, not duplicated here.

**Why this audit now:** the app's read-aloud feature
([`text_to_speech.md`](specifications/text_to_speech.md)) was built for a specific visually
impaired user. TTS covers spoken *output*. This audit covers everything else that user (or
any screen-reader/keyboard-only user) touches: the message form, the live chat log, error
states, color contrast, and motion — the parts of the app TTS doesn't reach.

**Method:** static review of `app/templates/chat.html`, `app/templates/bot_message.html`,
`app/static/{script.js,tts.js,styles.css}`, and the relevant routes in `app/main.py`
(rendering, rate-limit handling), checked against WCAG 2.2 AA. No automated scanner or
screen reader was run as part of this pass — treat the findings below as a thorough manual
read, and re-verify with `axe-core` and NVDA/VoiceOver (per the spec's §6) once fixes land,
per finding **A11Y-13**.

## Severity key

- **Critical** — blocks the app's core task for a screen-reader user.
- **High** — degrades the experience significantly but doesn't block task completion.
- **Medium** — a real WCAG gap, lower practical impact for this app's specific usage pattern.
- **Low** — minor polish or defense-in-depth; not a WCAG failure on its own.

## Findings

| ID | Severity | Location | Issue | WCAG | Spec § | Status |
|---|---|---|---|---|---|---|
| A11Y-1 | **Critical** | `chat.html` `#chat-container` | New messages (user echo + bot response) are appended via HTMX/`script.js` with no `aria-live`/`role="log"` on the container. A screen-reader user gets no indication a response arrived. | 4.1.3 Status Messages | 5.3 | **Fixed** — `role="log" aria-live="polite" aria-relevant="additions"` added to `#chat-container`. |
| A11Y-2 | **Critical** | `chat.html`, `#message-input` | The message `<input>` has only a `placeholder`, no `<label>`. Accessible name is unreliable across AT/browser combinations, and disappears once text is entered. | 1.3.1, 3.3.2, 4.1.2 | 5.2 | **Fixed** — visually-hidden `<label for="message-input">Your message</label>` added; verified the input's accessible name now resolves from the label. |
| A11Y-3 | High | `chat.html`, `<template id="typing-indicator">` | The bouncing-dots "typing" indicator has no text alternative or live-region announcement — a screen-reader user waits in silence with no indication the assistant is working. | 4.1.3 | 5.3 | **Fixed** — a visually-hidden "Assistant is composing a response" span was added inside the typing-indicator template; since it's appended into `#chat-container` (now a live region), it's announced automatically without separate wiring. |
| A11Y-4 | High | `app/main.py` `_rate_limit_exceeded_handler` (line ~134) | On `429`, the `/chat` HTMX request receives a raw `JSONResponse({"detail": ...})`. Since the form's `hx-target="#chat-container"`/`hx-swap="beforeend"` has no error handling, this can land as unstyled JSON text inside the chat log — unintelligible and (per A11Y-1) unannounced either way. | 3.3.1, 4.1.3 | 5.10 | **Fixed** — the handler now renders `app/templates/error_message.html` (plain-language sentence) instead of `JSONResponse`. Since htmx doesn't auto-swap non-2xx/3xx responses, a new `htmx:responseError` listener in `script.js` inserts the fragment into `#chat-container`, where A11Y-1's live region announces it; a non-HTML error response falls back to a generic message. |
| A11Y-5 | High | `chat.html`, whole document | No `<main>` landmark; the entire page is `<div class="container">`. Screen-reader landmark navigation has nothing to jump to. | 1.3.1, 2.4.1 | 5.1 | **Fixed** — the page's `<div class="container">` is now `<main class="container">`. |
| A11Y-6 | Medium | `styles.css` `.message`, `.dot`/`@keyframes bounce` | Fade/slide-in transition on every message and the infinite typing-indicator bounce animation have no `prefers-reduced-motion` guard. | 2.3.3 (AAA, but low-cost to fix) | 5.6 | **Fixed** — both are now wrapped in `@media (prefers-reduced-motion: no-preference)`, with the static end-state (`opacity:1; transform:none`, no animation) as the unconditional default. |
| A11Y-7 | Medium | `chat.html` `<h1>` → `bot_message.html` `<h6>Sources:</h6>` | Heading levels jump from `h1` straight to `h6` with nothing between; model-generated Markdown in `bot_response_html` can also introduce arbitrary heading levels with no relation to the page's own structure. | 1.3.1 (best practice; not a hard AA failure) | 5.1 | **Fixed** — `<h6>Sources:</h6>` changed to `<p class="card-subtitle ...">Sources:</p>` (same visual styling, no heading semantics). Model-generated Markdown headings are unchanged/out of scope. |
| A11Y-8 | Medium | `styles.css` (`text-muted` usage in `bot_message.html`'s "(response shortened)" note and `chat.html`'s `#tts-disclosure`) | `chat.html` pinned `bootstrap@5.3.0-alpha1` (a pre-release build) and relied on `data-bs-theme="dark"` to remap Bootstrap's `.text-muted` for dark backgrounds, unverified at the time. | 1.4.3 | 5.5 | **Fixed** — Bootstrap upgraded to `5.3.8` (stable). Re-measured 2026-07-26 against the live rendered page: `.text-muted` computes to `rgba(222,226,230,0.75)` over `#1a1a1a`, ≈**8:1** contrast — well above the 4.5:1 AA minimum. No manual override needed. |
| A11Y-9 | Low | `bot_message.html` Listen/Stop button, disabled variant | Disabled state uses both `title` and `aria-label` — the `aria-label` already carries the needed information to AT, so this is not a functional gap, just worth noting `title`-only tooltips are unreliable on touch/keyboard if ever relied on alone elsewhere. | — | 5.8 | No action needed. |
| A11Y-10 | Low | `chat.html` `#tts-disclosure` | The AI-voice disclosure text is visible near the controls but not programmatically linked to the Listen buttons via `aria-describedby`. Already satisfies `text_to_speech.md`'s policy requirement as visible text; this is a refinement, not a gap. | — | 5.9 | Not fixed — optional refinement, unchanged. |
| A11Y-11 | Medium | `app/static/script.js` (`htmx:beforeRequest` handler, line ~8) | User message is inserted via `userMessage.innerHTML = \`<p>${message}</p>\`` — raw user input into `innerHTML`. Flagged here because it also matters for AT: unescaped markup could alter the DOM structure a screen reader parses, though the primary concern is that this is an **XSS vulnerability**, out of scope for this accessibility document but worth a follow-up fix (use `textContent` or escape the string). | — (security, not WCAG) | — | **Fixed** — now builds the message with `createElement`/`textContent` instead of `innerHTML`. |
| A11Y-12 | Low | `chat.html` | No skip-to-content link. Acceptable for today's single-view page per the spec's §5.1 exception, but flagged so it isn't forgotten once a second landmark/nav element is added. | 2.4.1 | 5.1 | Not fixed — still an acceptable exception for the current single-view page. |
| A11Y-13 | Medium | project-wide | No automated accessibility testing (`axe-core` or equivalent) and no documented screen-reader/keyboard test pass exists in `tests/` or as a manual checklist prior to this document. | — (process gap) | 6 | Not fixed — remains open; see Phase 4. |

## Prioritized plan

Phases 1–3 below are implemented (see the **Status** column in the findings table above for
what changed in each file). Phase 4 remains open.

### Phase 1 — Critical: make the chat log itself accessible (done)

The two Critical items block the core task (holding a conversation) for a screen-reader
user; fix these before anything else here.

1. **A11Y-1 — Live-announce new messages.**
   - Add `role="log" aria-live="polite" aria-relevant="additions"` to `#chat-container` in
     `chat.html`.
   - Verify Bootstrap's `.chat-container` scroll behavior and the existing fade-in animation
     (A11Y-6) don't fight the live region — test with NVDA/VoiceOver that only the *new*
     message is spoken per turn, not the whole scrollback.
2. **A11Y-2 — Label the message input.**
   - Add `<label for="message-input" class="visually-hidden">Your message</label>`
     immediately before the `<input>` in `chat.html`'s form, matching the existing
     `audio-mode-checkbox` label pattern already in the same file.
   - Keep the `placeholder` — it's fine as supplementary hint text once a real label exists.

### Phase 2 — High: close the remaining functional gaps (done)

3. **A11Y-3 — Announce the typing/thinking state.**
   - Add a small `aria-live="polite"` region (reuse the per-message pattern from
     `tts-status-{{ message_id }}`) that's updated to "Assistant is composing a response"
     when the typing indicator appears, and cleared when it's removed. Wire this from the
     existing `htmx:beforeRequest`/`htmx:afterSwap` handlers in `script.js`, which already
     insert/remove the indicator at exactly the right moments.
4. **A11Y-4 — Render rate-limit (and other `/chat`-level) errors as accessible HTML.**
   - Have `_rate_limit_exceeded_handler` (or a wrapper specific to the `/chat` route)
     return an HTML fragment consistent with `bot_message.html`'s shape — a
     plain-language sentence ("You're sending messages a bit fast — please wait a moment
     and try again.") — instead of `JSONResponse`, when the request is the HTMX form
     submission. Ensure this fragment lands inside the now-live-region container from
     A11Y-1 so it's announced, not just visible.
5. **A11Y-5 — Add a `<main>` landmark.**
   - Wrap `chat.html`'s primary `<div class="container">` content in `<main>` (or add
     `role="main"` to it if restructuring the div is undesirable).

### Phase 3 — Medium: contrast, motion, structure (done)

6. **A11Y-6 — Respect reduced motion.**
   - Wrap `.message`'s transition and `.dot`'s `animation` in
     `@media (prefers-reduced-motion: no-preference) { ... }` in `styles.css`; provide the
     static end-state (`opacity: 1; transform: none`) as the default outside that query.
7. **A11Y-7 — Fix heading hierarchy.**
   - Change `bot_message.html`'s `<h6 class="card-subtitle ...">Sources:</h6>` to a
     non-heading element (e.g. a `<p>` or `<span>` styled identically via a class) unless a
     real intermediate heading level is introduced on the page — a citations label
     inside one message doesn't need to be a heading at all.
8. **A11Y-8 — Confirm (or fix) `text_to_speech` /`text-muted` contrast.**
   - Render the page and inspect the computed color of `.text-muted` against
     `data-bs-theme="dark"` in the actual pinned `bootstrap@5.3.0-alpha1` build. If it
     resolves below AA (§5.5's 4.5:1 threshold), override it explicitly in `styles.css`
     with a confirmed-compliant color rather than relying on the alpha dependency.

### Phase 4 — Low / follow-ups (still open)

9. **A11Y-9, A11Y-10** — no action required now; revisit only if a future redesign touches
   these areas.
10. ~~**A11Y-11**~~ — fixed alongside this work (see findings table); `script.js` now builds
    the user-message bubble with `createElement`/`textContent`.
11. **A11Y-12** — add a skip-link when a second landmark/nav element is introduced; not
    needed today.
12. **A11Y-13** — set up `axe-core` against rendered templates and document the manual
    keyboard/screen-reader pass from the spec's §6 as a pre-merge step for frontend PRs.
    **Still open** — this pass fixed the underlying issues but did not add automated
    coverage or a documented manual screen-reader run; that remains the one Phase 4 item
    worth prioritizing next.

## Acceptance criteria for calling this remediation done

1. A full conversation (read greeting, use a starter prompt, send a custom message, open
   Toggle Sources, use Listen/Stop) can be completed using only a keyboard and only a
   screen reader (NVDA or VoiceOver), with every state change — message arrival, typing
   indicator, errors — announced without the user needing to manually re-read the page.
   **Partially verified** — structural/DOM checks confirm the live region receives the
   typing indicator, bot response, and error fragment in the browser; an actual NVDA/VoiceOver
   pass has not yet been run.
2. Every interactive control has a real accessible name (no placeholder-only inputs, no
   unlabeled icon buttons). **Verified** — `#message-input`'s accessible name now resolves
   from its `<label>`.
3. No color pairing in active use falls below WCAG AA contrast minimums (§5.5), confirmed
   by direct inspection of rendered, computed colors — not just source CSS variables.
   **Verified** for `.text-muted` (≈8:1 post-upgrade); other pairs were not re-measured since
   they weren't flagged as at-risk.
4. Animations respect `prefers-reduced-motion`. **Fixed** — not yet confirmed with an actual
   OS-level reduced-motion toggle in a real browser session.
5. No user-facing error path renders raw JSON or an unstyled fragment. **Fixed** for the
   `/chat` rate-limit path (429 → HTML fragment, announced via the live region).
6. `axe-core` runs in CI against the rendered templates and the manual test pass in
   [`accessibility.md`](specifications/accessibility.md) §6 has been performed at least
   once against the fixes above. **Still open** — see A11Y-13 above.

Once every item above is fixed and re-verified, this document should either be deleted or
reduced to a changelog entry — the durable guidance stays in
[`accessibility.md`](specifications/accessibility.md), so this file shouldn't accumulate
indefinitely as a second source of truth.
