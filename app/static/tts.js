// Read-aloud (TTS) playback controller — docs/specifications/text_to_speech.md §5.2.
//
// Side-effect-free on import: this module only defines constants/TTSController.
// Wiring it to the live page happens in initTTSController(), called from a small
// inline module script in chat.html — kept separate so this file stays directly
// importable (and testable) from vitest without touching the real DOM on import.

export const TTS_STATE = {
  IDLE: "idle",
  LOADING: "loading",
  PLAYING: "playing",
  ERROR: "error",
};

export const TTS_STATUS_MESSAGE = {
  LOADING: "Generating audio…",
  PLAYING: "Playing audio",
  TOO_LONG: "This response is too long to read aloud in one piece.",
  RATE_LIMITED: "Please wait a moment before trying again.",
  UNAVAILABLE: "Audio unavailable.",
  IDLE: "",
};

const STATUS_MESSAGE_BY_HTTP_STATUS = {
  404: TTS_STATUS_MESSAGE.UNAVAILABLE,
  413: TTS_STATUS_MESSAGE.TOO_LONG,
  429: TTS_STATUS_MESSAGE.RATE_LIMITED,
};

export const TTS_BUTTON_LABEL = {
  IDLE: "\u{1F50A} Listen",
  LOADING: "Generating audio…",
  PLAYING: "⏹ Stop",
};

export const TTS_ARIA_LABEL = {
  IDLE: "Read this response aloud",
  PLAYING: "Stop reading aloud",
};

export const TTS_PLAYING_CLASS = "tts-toggle--playing";
export const TTS_TOGGLE_SELECTOR = ".tts-toggle";
export const TTS_ENABLED_TOGGLE_SELECTOR = ".tts-toggle:not([disabled])";

export const CHAT_CONTAINER_ID = "chat-container";
export const AUDIO_MODE_CHECKBOX_ID = "audio-mode-checkbox";
export const BOT_MESSAGE_CLASS = "bot-message";
export const HTMX_AFTER_SWAP_EVENT = "htmx:afterSwap";

// How long an error state is shown before reverting to idle, so a stale error
// message doesn't linger forever if the user doesn't click again.
export const TTS_ERROR_RESET_DELAY_MS = 4000;

/**
 * Per-message read-aloud state machine. One instance is shared across every
 * Listen/Stop button on the page (state is tracked internally per message id),
 * matching the spec's "only one message plays at a time" requirement.
 */
export class TTSController {
  constructor({
    fetchImpl,
    getButton,
    getAudio,
    getStatus,
    errorResetDelayMs = TTS_ERROR_RESET_DELAY_MS,
    setTimeoutImpl = (...args) => setTimeout(...args),
  } = {}) {
    this._fetch = fetchImpl || ((...args) => fetch(...args));
    this._getButton =
      getButton ||
      ((id) => document.querySelector(`${TTS_TOGGLE_SELECTOR}[data-message-id="${id}"]`));
    this._getAudio = getAudio || ((id) => document.getElementById(`tts-audio-${id}`));
    this._getStatus = getStatus || ((id) => document.getElementById(`tts-status-${id}`));
    this._errorResetDelayMs = errorResetDelayMs;
    this._setTimeout = setTimeoutImpl;
    this._entries = new Map();
  }

  getState(messageId) {
    return this._entry(messageId).state;
  }

  async handleClick(messageId) {
    const state = this.getState(messageId);
    if (state === TTS_STATE.LOADING) {
      this._cancel(messageId);
    } else if (state === TTS_STATE.PLAYING) {
      this._stop(messageId);
    } else {
      await this._start(messageId);
    }
  }

  /**
   * Starts playback for a message that hasn't been interacted with yet — used
   * for audio-mode auto-play on arrival. Unlike handleClick(), this never
   * cancels/stops an existing state; it's a no-op unless the message is IDLE,
   * so a program-triggered auto-play can never fight a click the user already
   * made (e.g. they started playing a different message themselves).
   */
  async play(messageId) {
    if (this.getState(messageId) !== TTS_STATE.IDLE) return;
    await this._start(messageId);
  }

  /** Stops/cancels every message's playback except the given one. */
  stopOthers(exceptMessageId) {
    for (const messageId of Array.from(this._entries.keys())) {
      if (messageId === exceptMessageId) continue;
      const state = this.getState(messageId);
      if (state === TTS_STATE.PLAYING) {
        this._stop(messageId);
      } else if (state === TTS_STATE.LOADING) {
        this._cancel(messageId);
      }
    }
  }

  /** Revokes every tracked object URL — call on page unload. */
  revokeAll() {
    for (const messageId of this._entries.keys()) {
      this._revoke(messageId);
    }
  }

  _entry(messageId) {
    let entry = this._entries.get(messageId);
    if (!entry) {
      entry = {
        state: TTS_STATE.IDLE,
        abortController: null,
        requestToken: 0,
        objectUrl: null,
        endedBound: false,
      };
      this._entries.set(messageId, entry);
    }
    return entry;
  }

  async _start(messageId) {
    this.stopOthers(messageId);
    const entry = this._entry(messageId);
    entry.abortController = new AbortController();
    const token = ++entry.requestToken;
    this._setState(messageId, TTS_STATE.LOADING);

    let response;
    try {
      response = await this._fetch(`/api/messages/${messageId}/speech`, {
        method: "POST",
        signal: entry.abortController.signal,
      });
    } catch (err) {
      if (err && err.name === "AbortError") return; // cancelled — not an error
      this._handleFailure(messageId, token, TTS_STATUS_MESSAGE.UNAVAILABLE);
      return;
    }

    if (!this._isCurrent(messageId, token)) return; // superseded by a newer click

    if (!response.ok) {
      const statusMessage =
        STATUS_MESSAGE_BY_HTTP_STATUS[response.status] || TTS_STATUS_MESSAGE.UNAVAILABLE;
      this._handleFailure(messageId, token, statusMessage);
      return;
    }

    let blob;
    try {
      blob = await response.blob();
    } catch (err) {
      this._handleFailure(messageId, token, TTS_STATUS_MESSAGE.UNAVAILABLE);
      return;
    }

    if (!this._isCurrent(messageId, token)) return;

    const objectUrl = URL.createObjectURL(blob);
    entry.objectUrl = objectUrl;
    const audio = this._getAudio(messageId);
    audio.src = objectUrl;
    this._bindEnded(messageId, audio);

    try {
      // audio.play() can reject independently of any onerror handler (some
      // browsers reject it on rapid interaction, or per autoplay policy if the
      // user gesture is judged to have "expired" during the network round trip).
      await audio.play();
    } catch (err) {
      if (this._isCurrent(messageId, token)) {
        this._handleFailure(messageId, token, TTS_STATUS_MESSAGE.UNAVAILABLE);
      } else {
        this._revoke(messageId);
      }
      return;
    }

    if (!this._isCurrent(messageId, token)) {
      audio.pause();
      this._revoke(messageId);
      return;
    }
    this._setState(messageId, TTS_STATE.PLAYING);
  }

  _bindEnded(messageId, audio) {
    const entry = this._entry(messageId);
    if (entry.endedBound) return;
    entry.endedBound = true;
    audio.addEventListener("ended", () => this._onEnded(messageId));
  }

  _onEnded(messageId) {
    if (this.getState(messageId) !== TTS_STATE.PLAYING) return;
    this._revoke(messageId);
    this._setState(messageId, TTS_STATE.IDLE);
  }

  _cancel(messageId) {
    const entry = this._entry(messageId);
    if (entry.abortController) entry.abortController.abort();
    entry.requestToken += 1; // invalidates any in-flight response, even post-abort
    this._revoke(messageId);
    this._setState(messageId, TTS_STATE.IDLE);
  }

  _stop(messageId) {
    const audio = this._getAudio(messageId);
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    this._revoke(messageId);
    this._setState(messageId, TTS_STATE.IDLE);
  }

  _handleFailure(messageId, token, statusMessage) {
    if (!this._isCurrent(messageId, token)) return;
    this._revoke(messageId);
    this._setState(messageId, TTS_STATE.ERROR, statusMessage);
    this._setTimeout(() => {
      if (this.getState(messageId) === TTS_STATE.ERROR) {
        this._setState(messageId, TTS_STATE.IDLE);
      }
    }, this._errorResetDelayMs);
  }

  _isCurrent(messageId, token) {
    return this._entry(messageId).requestToken === token;
  }

  _revoke(messageId) {
    const entry = this._entry(messageId);
    if (entry.objectUrl) {
      URL.revokeObjectURL(entry.objectUrl);
      entry.objectUrl = null;
    }
  }

  _setState(messageId, state, statusMessageOverride) {
    this._entry(messageId).state = state;

    const button = this._getButton(messageId);
    if (button) {
      if (state === TTS_STATE.PLAYING) {
        button.textContent = TTS_BUTTON_LABEL.PLAYING;
        button.setAttribute("aria-label", TTS_ARIA_LABEL.PLAYING);
        button.classList.add(TTS_PLAYING_CLASS);
      } else if (state === TTS_STATE.LOADING) {
        button.textContent = TTS_BUTTON_LABEL.LOADING;
        button.classList.remove(TTS_PLAYING_CLASS);
      } else {
        button.textContent = TTS_BUTTON_LABEL.IDLE;
        button.setAttribute("aria-label", TTS_ARIA_LABEL.IDLE);
        button.classList.remove(TTS_PLAYING_CLASS);
      }
    }

    const status = this._getStatus(messageId);
    if (status) {
      status.textContent =
        statusMessageOverride !== undefined
          ? statusMessageOverride
          : {
              [TTS_STATE.LOADING]: TTS_STATUS_MESSAGE.LOADING,
              [TTS_STATE.PLAYING]: TTS_STATUS_MESSAGE.PLAYING,
            }[state] || TTS_STATUS_MESSAGE.IDLE;
    }
  }
}

/**
 * Wires a TTSController to the live page via event delegation on #chat-container
 * (HTMX only ever appends bot messages here — never replaces — so a single
 * delegated listener stays correct without re-binding on every swap), auto-plays
 * a newly arrived response when audio mode is on, and revokes any outstanding
 * object URLs on page unload.
 */
export function initTTSController(controller = new TTSController()) {
  const chatContainer = document.getElementById(CHAT_CONTAINER_ID);
  if (chatContainer) {
    chatContainer.addEventListener("click", (event) => {
      const button = event.target.closest(TTS_TOGGLE_SELECTOR);
      if (!button || button.disabled) return;
      const messageId = button.dataset.messageId;
      if (!messageId) return;
      controller.handleClick(messageId);
    });
  }

  // Autoplay-on-arrival: a user who has opted into "shorter replies for
  // listening" has told the app they intend to listen, which is a stronger
  // basis for auto-playing than guessing on every message regardless of
  // intent (docs/specifications/text_to_speech.md §3.1). Re-checked on every
  // swap (not just once) since the checkbox can be toggled between messages.
  // Note: because this play() call isn't inside a direct click handler, some
  // browsers' autoplay policy may still reject it — that's handled the same
  // as any other playback failure (an ERROR state), not a crash.
  document.body.addEventListener(HTMX_AFTER_SWAP_EVENT, () => {
    autoPlayLatestMessageIfEnabled(controller);
  });

  window.addEventListener("beforeunload", () => controller.revokeAll());
  return controller;
}

function autoPlayLatestMessageIfEnabled(controller) {
  const checkbox = document.getElementById(AUDIO_MODE_CHECKBOX_ID);
  if (!checkbox || !checkbox.checked) return;

  const chatContainer = document.getElementById(CHAT_CONTAINER_ID);
  const latestMessage = chatContainer && chatContainer.lastElementChild;
  if (!latestMessage || !latestMessage.classList.contains(BOT_MESSAGE_CLASS)) return;

  const button = latestMessage.querySelector(TTS_ENABLED_TOGGLE_SELECTOR);
  const messageId = button && button.dataset.messageId;
  if (!messageId) return;

  controller.play(messageId);
}
