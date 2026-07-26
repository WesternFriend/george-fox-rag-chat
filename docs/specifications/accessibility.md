# Accessibility Specification

Status: **guidelines, adopted 2026-07-26**. This is the durable reference for how this app
approaches accessibility going forward — conformance target, the patterns this codebase
should follow, and how to verify them. The concrete audit findings and remediation backlog
for the app *as it stands today* live separately in
[`accessibility-improvements.md`](../accessibility-improvements.md), which this spec's §6
testing process and §7 checklist are meant to keep from regressing once addressed.

## 1. Purpose

This app exists to let a visually impaired user hold a Quaker-style reflective conversation
with an AI companion. [`text_to_speech.md`](text_to_speech.md) already ships one major piece
of that — spoken responses — driven by that exact use case. But read-aloud output is one
feature, not the whole surface: the message form, the chat log, session errors, and every
future page need to work for a screen-reader user navigating by keyboard alone, not just for
a sighted user who additionally wants to listen. This spec sets the conformance target and
the house patterns for getting there, so future changes to `chat.html`, `bot_message.html`,
`script.js`, and `styles.css` don't quietly regress what TTS was built to serve.

## 2. Conformance target

**WCAG 2.2, Level AA**, as the baseline for all user-facing pages and partials. Level AAA
success criteria (e.g. 2.3.3 Animation from Interactions) are followed where the cost is low
(this app's animations are decorative, not load-bearing), but AA is the bar a PR is expected
to clear, not AAA.

This app's actual audience skews toward assistive-technology reliance harder than a typical
AA target implies — the stated reason this feature line of work exists is a specific blind
user. Where a judgment call must be made between "minimum AA compliance" and "what actually
works well with a screen reader in practice," prefer the latter, and say so in the PR
description rather than silently picking the minimum.

## 3. Scope

Every page and HTMX-rendered partial this app serves: `chat.html` (the shell), all future
top-level pages, `bot_message.html` (swapped into `#chat-container` on every turn), and any
error/status content rendered in response to a request (rate limiting, validation errors,
upstream failures). Out of scope: the OpenAI-hosted TTS voice quality itself (covered by
`text_to_speech.md` §4.2's own audio-quality corpus) and the content of George Fox's source
texts (`texts/`), which this spec doesn't rewrite.

## 4. Architecture-specific accessibility model

This app's frontend shape — server-rendered HTML partials, swapped in by HTMX, no client
framework — determines how accessibility has to be done here, differently from a
client-rendered SPA:

- **New content arrives by DOM insertion, not page navigation.** Every bot response, and
  every user-message echo `script.js` inserts client-side, is a `beforeend` append into
  `#chat-container` (`chat.html`'s form, `script.js`'s `htmx:beforeRequest`/`htmx:afterSwap`
  handlers). A sighted user sees it appear; a screen-reader user gets nothing unless the
  container (or the inserted node) is inside a live region. **This is the single most
  consequential pattern in this spec** — see §5.3.
- **HTMX never replaces existing message nodes, only appends** (confirmed in
  `text_to_speech.md` §5.2's "HTMX-swap safety" note, still true). This is what makes
  per-message state (`tts.js`'s `TTSController`, keyed by `message_id`) safe without a
  defensive "is this node still attached" check — and it's also why a `role="log"` live
  region (§5.3) is the right pattern here rather than a full-region `aria-live` re-announce:
  `role="log"` is specifically designed for append-only, chronological content where only
  the new addition should be spoken, not the whole history re-read.
- **Server-rendered partials mean accessibility markup is a template-authoring
  responsibility, not a client-side afterthought.** `aria-live`, labels, and landmark
  elements belong in `chat.html`/`bot_message.html` themselves — there's no virtual-DOM
  diffing layer to retrofit them onto later. Get the template right the first time a new
  partial is added.
- **This app already has one correct example of the pattern this spec asks for
  everywhere else**: `bot_message.html`'s per-message
  `<div class="visually-hidden" role="status" aria-live="polite" id="tts-status-{{ message_id }}">`
  (`text_to_speech.md` §5.1). New dynamic-content work should read as an extension of that
  existing pattern, not an unrelated one.

## 5. Guidelines

### 5.1 Semantic structure and landmarks

- Use one `<main>` landmark wrapping the primary page content, and `<header>`/`<footer>` where
  applicable, instead of bare `<div class="container">` — a screen-reader user's landmark
  navigation (e.g. NVDA/JAWS "next region", VoiceOver rotor) should be able to jump straight
  to the chat interface.
- Maintain a single, logical heading level sequence per page (`h1` → `h2` → `h3` …, no skipped
  levels). Where a visual design calls for smaller-looking text at a "wrong" heading depth,
  style it with a class, don't skip the semantic level to get there.
- Content the RAG model generates (Markdown rendered via `markdown2`) can itself introduce
  headings (`##`, `###` in the model's own output). Treat model-generated headings as
  *nested under* the page's own heading structure conceptually, even though they render into
  a `<div class="message-content">` with no enclosing heading of their own today — don't let
  a citation subheading (e.g. "Sources:") sit at a heading depth that implies it's a sibling
  of the page's `<h1>` rather than a label within one message.
- A skip-to-content link is not required for this single-view app today (there's effectively
  one thing to skip to), but add one the moment a second landmark-worthy section, a nav bar,
  or a persistent header appears — don't wait for a complaint to retrofit it.

### 5.2 Forms and labels

- Every form control gets a programmatically associated label — a visible `<label for=...>`
  by default; a visually-hidden one (Bootstrap's `.visually-hidden` or `.form-label
  visually-hidden`) only when a visible label would be genuinely redundant with adjacent
  text, never as a first resort. `placeholder` text is not a substitute for a `<label>`: it
  disappears on input, isn't consistently exposed as an accessible name across
  browser/AT combinations, and fails WCAG 3.3.2/1.3.1 on its own.
- `chat.html`'s existing `<label class="form-check-label" for="audio-mode-checkbox">` is the
  pattern to match — every future control follows this shape, including ones that currently
  don't (see `accessibility-improvements.md` for the message input specifically).
- Required fields use the native `required` attribute (already the case for the message
  input) rather than a JS-only check — native HTML5 validation is announced by AT without
  extra work.
- Error messages (validation failures, rate-limit responses, upstream failures) are
  associated with the relevant control via `aria-describedby` when they're field-specific, or
  surfaced through a live region (§5.3) when they're response-level (e.g. a `429` on the
  whole `/chat` submission) — never conveyed by color or icon alone.

### 5.3 Live regions and dynamic content

This is the app's highest-leverage accessibility category, given §1's stated audience and §4's
append-only architecture.

- **The chat log itself** (`#chat-container`) should be a `role="log"` region (optionally
  `aria-live="polite"` `aria-relevant="additions"` as a belt-and-suspenders pairing, since
  `role="log"` support varies by AT) so that every appended user-echo and bot-response node
  is announced as it arrives, without re-announcing the entire scrollback on each turn.
  `role="log"` exists precisely for chronological, append-only content like this — it is the
  correct primitive, not a generic `aria-live="polite"` wrapper improvised for the purpose.
- **Transient status** (typing/thinking indicator, "message sent") uses a separate, small
  `aria-live="polite"` region with real text content ("Assistant is composing a response"),
  not a purely visual bouncing-dots indicator with no text alternative — a screen-reader
  user should learn the assistant is working the same moment a sighted user sees the dots.
- **Use `aria-live="assertive"` sparingly, and only for genuinely urgent, user-blocking
  information** (e.g. "your session expired, please reload") — routine content (a normal bot
  response) stays `polite` so it doesn't interrupt whatever the user's AT is already reading.
- **Per-message status regions already exist for TTS** (`tts-status-{{ message_id }}`,
  `text_to_speech.md` §5.1) — reuse this same pattern (a small, persistent, initially-empty
  `polite` live region near the thing it describes) for any new per-message dynamic state,
  rather than inventing a second convention.
- **Every error response the user can trigger** — a rate limit, a validation failure, an
  upstream timeout — must render as accessible, readable HTML inside (or announced by) a live
  region, never as raw JSON or an unstyled fragment inserted into the page. A `hx-target`
  swap that surfaces a bare `{"detail": "..."}` string is a failure on this axis even before
  it's a cosmetic one: it's unannounced *and* unintelligible if it is picked up.

### 5.4 Focus management

- Sending a chat message keeps focus in the message input (the correct behavior for a chat
  UI — a screen-reader user composing a follow-up shouldn't have focus torn away to the new
  response); the *announcement* of the new content is §5.3's job, not focus movement's.
- Reserve deliberate focus movement for moments a sighted user's attention would also
  meaningfully move — e.g. the existing starter-prompt buttons already do this correctly
  (`script.js`: filling the input, then `scrollIntoView` + `.focus()`), because clicking one
  is an action whose result the user needs to see/edit immediately.
- Never suppress the browser's default focus indicator (no bare `outline: none` in
  `styles.css`) unless it's immediately replaced with an equally or more visible custom focus
  style. Today's stylesheet doesn't do this — keep it that way as new interactive elements
  are added.
- Any future modal, dialog, or off-canvas panel must trap focus while open and restore it to
  the triggering control on close (native `<dialog>` or Bootstrap's own modal component both
  handle this correctly out of the box — don't hand-rock a custom overlay without it).

### 5.5 Color and contrast

- Meet WCAG AA contrast minimums against this app's actual dark theme, not against
  Bootstrap's light-mode defaults: **4.5:1 for normal text, 3:1 for large text (≥24px, or
  ≥18.66px bold) and for UI component boundaries/focus indicators.**
- When adding or changing a CSS custom property in `:root` (`styles.css`), check its
  contrast against every background it's actually painted on in this app (message bubbles,
  page body, form controls) — not just one of them. `--text-color: #e0e0e0` measures well
  above AA against all three backgrounds it's used on today (`#1a1a1a`, `#2c2c2c`, `#242424`
  — all >10:1); keep new text colors at or above that bar.
- Bootstrap utility classes that imply "muted"/secondary emphasis (`.text-muted`, and similar)
  are calibrated against Bootstrap's *light*-mode palette by default. This app sets
  `data-bs-theme="dark"` on `<body>`, which Bootstrap ≥5.3 remaps correctly for dark
  backgrounds. `chat.html` pins `bootstrap@5.3.8` (stable); as rendered, `.text-muted`
  measures ≈8:1 against this app's body color, well above the 4.5:1 normal-text minimum. If a
  future Bootstrap upgrade ever regresses this remap (or a muted-text token resolves toward
  Bootstrap's light-mode default, `#6c757d` — which fails AA on a dark background at ≈3.7:1),
  override it explicitly in `styles.css` rather than trusting the dependency blindly.
- Never convey state through color alone. The existing `.tts-toggle--playing` treatment
  already does this right (background color change *and* label text change from "Listen" to
  "Stop") — match that shape for any new stateful control.

### 5.6 Motion and animation

- Wrap purely decorative motion — the message fade/slide-in transition, the typing
  indicator's bounce animation — in `@media (prefers-reduced-motion: no-preference)`, so a
  user with a system-level reduced-motion preference (relevant for vestibular disorders, and
  a real subset of this app's own visually-impaired audience) gets an instant, static
  equivalent instead. This app has none of this today; treat it as a straightforward,
  low-risk addition rather than a redesign — the animation is cosmetic, not information-
  bearing, so a reduced-motion fallback loses nothing.
- An animation that repeats indefinitely while the user waits (the bounce dots) is a
  candidate to pause or slow under reduced motion even if it isn't removed outright, since
  WCAG's concern is with motion that's large, fast, or unpredictable enough to trigger
  discomfort — a slower or static "thinking" indicator communicates the same state without
  that risk.

### 5.7 Keyboard operability

- Every interactive control (send button, starter-prompt buttons, Toggle Sources, Listen/
  Stop, the audio-mode checkbox, any future control) must be reachable and operable via
  `Tab`/`Shift+Tab` and activated via `Enter`/`Space`, with no positive `tabindex` values and
  no keyboard traps. This app's controls are all native `<button>`/`<input>`/Bootstrap
  collapse today, which get this for free — preserve that by continuing to prefer native
  interactive elements over `<div onclick>`-style custom controls.
- Bootstrap's collapse (`data-bs-toggle="collapse"`, used by "Toggle Sources") and the
  `<details>`/`<summary>` pattern (used per-citation inside the sources panel) are both
  correct, keyboard-native choices already in this codebase — keep reaching for these
  primitives rather than JS-only show/hide toggles when adding similar disclosure UI.
- Verify tab order visually matches reading/DOM order after any layout change — CSS
  reordering (flex/grid `order`, absolute positioning) that diverges from source order is a
  common way keyboard order silently breaks even when each control is individually
  operable.

### 5.8 Non-text content: icons, emoji, and images

- Any icon-only or emoji-bearing control gets an explicit `aria-label` describing the
  *action*, not the glyph — `bot_message.html`'s Listen/Stop button
  (`aria-label="Read this response aloud"` / programmatically updated to `"Stop reading
  aloud"` per `text_to_speech.md` §5.1, `tts.js`'s `TTS_ARIA_LABEL`) is the reference example;
  match this shape for any new icon/emoji control rather than relying on the glyph alone,
  since emoji rendering and AT verbosity vary by platform.
- Purely decorative glyphs (an icon next to text that already conveys the same meaning) get
  `aria-hidden="true"` so AT doesn't announce redundant or nonsensical glyph names.
- Any future informational image (not decorative) gets real `alt` text describing its
  content/purpose; a decorative one gets `alt=""` (empty, not omitted) so AT skips it
  cleanly.

### 5.9 Audio narration (text-to-speech) accessibility

The read-aloud feature itself is specified in full in
[`text_to_speech.md`](text_to_speech.md), including its own accessibility requirements
(explicit `aria-label`s that update with state, per-message live regions, keyboard
activation, visible AI-voice disclosure — see that spec's §3 and §12 acceptance criteria).
This section only adds what's specific to *this* spec's scope:

- TTS is a complement to, not a substitute for, the rest of this document — a screen-reader
  user must be able to use the entire app (compose a message, read a response, navigate
  sources) via their AT alone, with or without ever clicking Listen. Don't let "well, they
  can just listen to it" become a reason to under-invest in the underlying HTML's own
  accessibility.
- The visible AI-voice disclosure (`#tts-disclosure` in `chat.html`) satisfies
  `text_to_speech.md`'s policy requirement as visible text; associating it more tightly with
  the audio controls (e.g. `aria-describedby` on the Listen buttons) is a nice-to-have
  refinement, not a blocking gap — the disclosure is already discoverable by any means of
  reading the page.

### 5.10 Error states and status messages

- Every user-triggerable error (rate limiting, a form-validation failure, an upstream `502`/
  `504` from the chat-completion or TTS call) renders as real, readable HTML — a sentence a
  person can understand — inside or announced by a live region (§5.3), never a bare JSON
  body or an HTTP status code with no HTML wrapper landing in `#chat-container`.
- Match the specificity already established for TTS errors (`tts.js`'s
  `STATUS_MESSAGE_BY_HTTP_STATUS`, mapping `404`/`413`/`429` to distinct human sentences) for
  the main `/chat` flow's own error paths — a generic "Something went wrong" is acceptable
  only as a last-resort fallback, not the default for every failure mode.

## 6. Testing and verification

Accessibility is verified the same way this codebase already verifies behavior — automated
tests plus a documented manual pass — not left to a one-time audit.

**Automated:**

- Add `axe-core` (via `@axe-core/playwright` or `jest-axe`/`vitest-axe`, matching this
  project's existing `vitest` frontend setup) to run against rendered `chat.html` and
  `bot_message.html` output, catching regressions in labeling, contrast (where computable
  statically), and landmark/heading structure automatically in CI.
- Where feasible, assert on the specific patterns this spec calls out directly in
  `tests/test_main.py` (BeautifulSoup already used there) — e.g. that `#chat-container` has
  `role="log"`, that the message input has an associated label, that error responses render
  as HTML with a live region rather than raw JSON.

**Manual (run before shipping any change to `chat.html`, `bot_message.html`, `script.js`, or
`styles.css`):**

- A full task (read the greeting, use a starter prompt, send a custom message, open Toggle
  Sources, use Listen/Stop) completed keyboard-only, no mouse.
- The same task completed with a screen reader — at minimum one of NVDA (Windows) or
  VoiceOver (macOS/iOS), since this app's actual motivating user is a screen-reader user;
  JAWS if available.
- Zoom the page to 200% and confirm no content or functionality is lost (WCAG 1.4.10 Reflow).
- Toggle the OS-level reduced-motion setting and confirm animations respond (§5.6).
- Run a contrast checker against any new or changed color pair, not just the ones already
  audited in §5.5.

## 7. Definition of done for future PRs touching the frontend

A PR that adds or changes markup in `chat.html`/`bot_message.html`, styling in `styles.css`,
or DOM behavior in `script.js`/`tts.js` should be able to answer "yes" to each of:

1. Does every new interactive control have an accessible name (visible label, `aria-label`,
   or equivalent) — not a placeholder alone?
2. Does every new piece of dynamically-inserted content get announced to a screen-reader
   user, via an existing or new live region (§5.3) — not just visually appended?
3. Is every new control keyboard-reachable and operable without a mouse?
4. Does every new color pairing meet the contrast minimums in §5.5, checked against this
   app's actual dark theme?
5. Does any new animation respect `prefers-reduced-motion`?
6. Does any new error/status path render as readable HTML, not raw JSON or an unstyled
   fragment?

## 8. Non-goals

- This spec doesn't mandate a specific automated-testing tool beyond the guidance in §6 —
  `axe-core` is a strong default recommendation, not a hard requirement if the team already
  has an equivalent in place.
- This spec doesn't cover the accessibility of third-party-hosted dependencies this app
  merely loads (the Bootstrap/HTMX CDN bundles) beyond how this app's own markup uses them —
  Bootstrap's own components (collapse, form controls) are treated as accessible building
  blocks, consistent with §5.7's guidance to prefer them over custom equivalents.

## 9. References

- [WCAG 2.2](https://www.w3.org/TR/WCAG22/) — the conformance target (§2).
- [WAI-ARIA Authoring Practices Guide](https://www.w3.org/WAI/ARIA/apg/) — patterns for
  live regions, disclosure widgets, and custom controls referenced throughout §5.
- [`text_to_speech.md`](text_to_speech.md) — the read-aloud feature this spec complements,
  not duplicates (§5.9).
- [`accessibility-improvements.md`](../accessibility-improvements.md) — the current audit
  findings and phased remediation plan for this app against the guidelines above.
