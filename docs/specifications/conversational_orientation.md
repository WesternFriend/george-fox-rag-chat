# Conversational Orientation Specification

Status: implemented — initial pass (landing-page copy in `app/templates/chat.html`, starter
queries in `app/static/script.js`/`app/static/styles.css`, and the closing-query instruction
in `app/prompts/system_prompt.md`). This document records the orientation those changes
express, so future UX and prompt work stays coherent with it rather than drifting back
toward a generic Q&A tone one small edit at a time.

## 1. Problem statement

Before this pass, the app framed itself as a lookup tool: page title "George Fox writings",
heading "George Fox and Quakerism chat", intro "Ask me anything about George Fox and
Quakerism," and an opening bot line of "Hello! How are you? How can I assist you today?"
That's a closed-ended, pedagogical framing — it invites trivia questions with correct
answers ("When was George Fox born?"), and gives a visitor with no trivia question in mind
nothing to do. Several clauses in `app/prompts/system_prompt.md` already pushed against superficial,
slogan-dropping answers (see the "Naming a conviction is not the same as offering insight"
clause), but the surrounding UX still framed the tool as an encyclopedia, not an invitation.

## 2. The orientation

This app is an **open-ended inquiry companion**, not a closed-ended pedagogical tool. The
distinction:

| Closed-ended / pedagogical | Open-ended / inquiry |
|---|---|
| Answers questions about Fox and Quaker history | Helps a person sit with what's actually on their mind |
| Historic texts are the destination ("here's what Fox said") | Historic texts are material brought *into* the person's situation |
| Success = a correct, complete answer | Success = the person sees their situation a little differently, or names something they hadn't named |
| Tone: informative | Tone: companionable, gathered, unhurried |

Two pieces of genuine Quaker practice ground this, rather than it being generic chatbot
"be more engaging" advice:

- **Queries.** Friends have long used queries — searching questions held in worship, not
  answered and closed off — to test what's true in a situation rather than resolve it
  neatly. `app/prompts/system_prompt.md` now instructs the model to close a response with one genuine,
  open-ended query when the conversation has room to go further (not a rhetorical "anything
  else?"), mirroring this practice.
- **Continuing revelation.** Early Friends held that Truth didn't stop unfolding with
  scripture or with Fox's own generation — it continues to open in the present, including
  within the person asking. That's why the retrieved historic passages are framed (both in
  the landing copy and in `app/prompts/system_prompt.md`) as material the conversation works *with*,
  brought alongside the person's own situation — not as settled answers handed down from
  authority. A quotation should do interpretive work on what the person is facing, not
  stand alone as the answer.

The landing page should read as an invitation into that kind of space — closer to how a
meeting for worship or a threshing session opens than how a search engine's homepage does —
without literally roleplaying a meeting for worship.

## 3. Implementation surfaces

**Landing copy** (`app/templates/chat.html`):
- Title: "A Quaker companion for reflection" (was "George Fox writings").
- Heading: "A companion for going deeper" (was "George Fox and Quakerism chat").
- Intro line names the reorientation directly: "This isn't a trivia tool... Bring what's
  actually on your mind."
- Opening bot message names the query tradition, asks what's actually on the visitor's
  heart or mind, and offers four starter-query buttons (`starter-prompt-btn`, wired in
  `script.js`) covering a hard decision, a strained relationship, a quiet doubt, and
  simplicity — concrete, low-effort entry points for a visitor with nothing specific in
  mind yet. Buttons fill `#message-input` and focus it; they never auto-submit, so the
  visitor can personalize the prompt or just send it as-is.

**System prompt** (`app/prompts/system_prompt.md`):
- Closing-query clause (§2 above).
- Pre-existing "read between the lines" / anti-cliché clause continues to carry most of
  the "bring real insight, not a slogan" weight — this spec doesn't change it, just gives
  it a name (open-ended inquiry vs. closed pedagogy) and extends the same spirit to the
  conversation's shape, not just individual answers.
- Pre-existing verbatim-quotation-with-attribution requirement is the concrete mechanism
  for "sources as material brought into the conversation," not restated here.

## 4. Guardrails

Leaning toward warmth, depth, and personal disclosure raises the surface area for harm if
taken too far. This spec does **not** change the app's existing boundary
(`app/prompts/system_prompt.md`'s "decline messages that are harmful or cruel... there is something sacred
in every person" clause) and does not ask the model to take on a therapist, spiritual
director, or crisis-counselor role. The queries this app offers are the same kind Friends
use in worship-sharing and business meeting — reflective, not clinical. If a conversation
moves into territory a caring companion couldn't responsibly hold alone (self-harm, crisis),
the existing decline-and-redirect behavior is the backstop; this spec doesn't add new
guidance there and a future pass should if real usage shows it's needed.

## 5. What this doesn't cover (yet)

- **No memory of themes across a session or across sessions.** Each query the model offers
  is grounded in the immediate exchange only; it doesn't yet track a recurring theme a
  visitor keeps returning to. `app/session.py`'s `SessionState` already holds the session's
  full `chat_history`, so a future pass could have the model notice and gently name a
  recurring thread — out of scope here.
- **No closing/epistle-style wrap-up** for a session (the way a meeting might close with a
  brief epistle). Not requested; noted as a possible future extension of the same
  orientation.

## 6. Evaluating whether this is working

Unlike citation-presence or archaic-language checks, tone and depth aren't mechanically
testable — no test asserts a response "felt like fellowship." Treat this as a matter for
periodic manual review of real transcripts (does the model reach for a closing query
naturally, or does it feel bolted-on and repetitive; do quotations do real interpretive
work or sit inert), not automation. `tests/test_main.py` continues to cover the
mechanically-checkable pieces this spec touches indirectly (citation presence, prompt
wiring) but does not and should not try to assert on prose tone.
