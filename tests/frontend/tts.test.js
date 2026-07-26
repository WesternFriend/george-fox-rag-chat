import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  TTSController,
  TTS_STATE,
  TTS_STATUS_MESSAGE,
  TTS_BUTTON_LABEL,
  TTS_ARIA_LABEL,
  HTMX_AFTER_SWAP_EVENT,
  initTTSController,
} from "../../app/static/tts.js";

function makeButton() {
  const button = document.createElement("button");
  button.className = "tts-toggle";
  button.setAttribute("aria-label", TTS_ARIA_LABEL.IDLE);
  return button;
}

function makeFakeAudio({ playImpl } = {}) {
  // A plain Element stands in for <audio> — jsdom's real HTMLMediaElement.play()
  // throws "not implemented", so playback is faked here instead. Elements still
  // support addEventListener/dispatchEvent, which is all _bindEnded needs.
  const el = document.createElement("div");
  el.play = vi.fn(playImpl || (() => Promise.resolve()));
  el.pause = vi.fn();
  el.currentTime = 0;
  el.src = "";
  return el;
}

function blobResponse() {
  return {
    ok: true,
    status: 200,
    blob: async () => new Blob(["fake-audio-bytes"]),
  };
}

function errorResponse(status) {
  return {
    ok: false,
    status,
    json: async () => ({ detail: "error" }),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

function makeHarness({ fetchImpl, playImpl, errorResetDelayMs = 10, setTimeoutImpl } = {}) {
  const button = makeButton();
  const status = document.createElement("div");
  const audio = makeFakeAudio({ playImpl });
  const controller = new TTSController({
    fetchImpl: fetchImpl || vi.fn(),
    getButton: () => button,
    getAudio: () => audio,
    getStatus: () => status,
    errorResetDelayMs,
    setTimeoutImpl,
  });
  return { controller, button, status, audio };
}

beforeEach(() => {
  global.URL.createObjectURL = vi.fn(() => "blob:mock-url");
  global.URL.revokeObjectURL = vi.fn();
  // initTTSController() attaches listeners to document.body itself, which
  // (unlike its children) survives a plain `document.body.innerHTML = ...`
  // reset — replace the body outright so listeners never leak between tests.
  document.documentElement.innerHTML = "<body></body>";
});

describe("TTSController happy path", () => {
  it("goes idle -> loading -> playing, then back to idle on natural end", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller, button, status, audio } = makeHarness({ fetchImpl });

    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);

    await controller.handleClick("m1");

    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);
    expect(button.textContent).toBe(TTS_BUTTON_LABEL.PLAYING);
    expect(button.getAttribute("aria-label")).toBe(TTS_ARIA_LABEL.PLAYING);
    expect(status.textContent).toBe(TTS_STATUS_MESSAGE.PLAYING);
    expect(audio.play).toHaveBeenCalledTimes(1);
    expect(audio.src).toBe("blob:mock-url");

    audio.dispatchEvent(new Event("ended"));

    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);
    expect(button.textContent).toBe(TTS_BUTTON_LABEL.IDLE);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
  });

  it("updates the .tts-toggle-label span in place rather than replacing it", async () => {
    // Mirrors bot_message.html's real markup: <button><span class="tts-toggle-label">...
    const button = makeButton();
    const label = document.createElement("span");
    label.className = "tts-toggle-label";
    label.textContent = TTS_BUTTON_LABEL.IDLE;
    button.appendChild(label);

    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const controller = new TTSController({
      fetchImpl,
      getButton: () => button,
      getAudio: () => makeFakeAudio(),
      getStatus: () => document.createElement("div"),
    });

    await controller.handleClick("m1");

    expect(button.contains(label)).toBe(true); // span survived the state change
    expect(label.textContent).toBe(TTS_BUTTON_LABEL.PLAYING);
  });

  it("play() starts playback for an idle message, same as a click", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller } = makeHarness({ fetchImpl });

    await controller.play("m1");

    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);
  });

  it("play() is a no-op if the message isn't idle (never fights an existing click)", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller } = makeHarness({ fetchImpl });

    await controller.handleClick("m1"); // user already started it — now PLAYING
    fetchImpl.mockClear();

    await controller.play("m1");

    expect(fetchImpl).not.toHaveBeenCalled();
    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);
  });

  it("shows the loading state while the request is in flight", async () => {
    const { promise, resolve } = deferred();
    const fetchImpl = vi.fn().mockReturnValue(promise);
    const { controller, button, status } = makeHarness({ fetchImpl });

    const clickPromise = controller.handleClick("m1");
    expect(controller.getState("m1")).toBe(TTS_STATE.LOADING);
    expect(button.textContent).toBe(TTS_BUTTON_LABEL.LOADING);
    expect(status.textContent).toBe(TTS_STATUS_MESSAGE.LOADING);

    resolve(blobResponse());
    await clickPromise;

    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);
  });
});

describe("cancel / stop", () => {
  it("cancels an in-flight request and ignores its eventual (stale) result", async () => {
    const { promise, resolve } = deferred();
    const fetchImpl = vi.fn().mockReturnValue(promise);
    const { controller } = makeHarness({ fetchImpl });

    const firstClick = controller.handleClick("m1");
    expect(controller.getState("m1")).toBe(TTS_STATE.LOADING);

    await controller.handleClick("m1"); // second click while loading => cancel
    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);

    resolve(blobResponse()); // the original request finally resolves — must be discarded
    await firstClick;

    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);
  });

  it("a stale invocation's late play() settlement only cleans up its own URL, never a newer invocation's", async () => {
    // Distinct URLs per call — the default beforeEach mock returns a fixed
    // string for every call, which would make this invocation's own URL and
    // a newer invocation's URL indistinguishable and defeat the test.
    let urlCounter = 0;
    global.URL.createObjectURL = vi.fn(() => `blob:mock-url-${++urlCounter}`);

    const playDeferredA = deferred();
    let playCalls = 0;
    const playImpl = () => {
      playCalls += 1;
      return playCalls === 1 ? playDeferredA.promise : Promise.resolve();
    };
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller, audio } = makeHarness({ fetchImpl, playImpl });

    const clickA = controller.handleClick("m1"); // A: fetch/blob resolve, then hangs at play()
    await new Promise((r) => setTimeout(r, 0)); // flush microtasks so A reaches await audio.play()
    expect(controller.getState("m1")).toBe(TTS_STATE.LOADING);
    const urlA = audio.src;

    await controller.handleClick("m1"); // cancel A (2nd click while loading)
    await controller.handleClick("m1"); // start + finish B fresh (3rd click, now idle)
    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);
    const urlB = audio.src;
    expect(urlB).not.toBe(urlA);

    audio.pause.mockClear();
    global.URL.revokeObjectURL.mockClear();

    playDeferredA.resolve(); // A's now-stale play() call finally settles
    await clickA;

    // B's playback must be completely undisturbed by A's late cleanup.
    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);
    expect(audio.src).toBe(urlB);
    expect(audio.pause).not.toHaveBeenCalled();
    expect(global.URL.revokeObjectURL).not.toHaveBeenCalledWith(urlB);
  });

  it("swallows a genuine AbortError from a cancelled fetch without an error state", async () => {
    const fetchImpl = vi.fn((_url, { signal }) => {
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      });
    });
    const { controller, status } = makeHarness({ fetchImpl });

    const clickPromise = controller.handleClick("m1");
    await controller.handleClick("m1"); // cancel — aborts the controller's signal
    await clickPromise;

    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);
    expect(status.textContent).toBe("");
  });

  it("stops playback and resets state on a click while playing", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller, audio } = makeHarness({ fetchImpl });

    await controller.handleClick("m1");
    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);

    await controller.handleClick("m1");

    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);
    expect(audio.pause).toHaveBeenCalled();
    expect(global.URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("stops another message's playback when a new one starts", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const elementsById = { m1: makeElements(), m2: makeElements() };
    const controller = new TTSController({
      fetchImpl,
      getButton: (id) => elementsById[id].button,
      getStatus: (id) => elementsById[id].status,
      getAudio: (id) => elementsById[id].audio,
    });

    await controller.handleClick("m1");
    expect(controller.getState("m1")).toBe(TTS_STATE.PLAYING);

    await controller.handleClick("m2");

    expect(controller.getState("m2")).toBe(TTS_STATE.PLAYING);
    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);
    expect(elementsById.m1.audio.pause).toHaveBeenCalled();
  });
});

function makeElements() {
  return { button: makeButton(), status: document.createElement("div"), audio: makeFakeAudio() };
}

describe("error handling", () => {
  it.each([
    [404, TTS_STATUS_MESSAGE.UNAVAILABLE],
    [413, TTS_STATUS_MESSAGE.TOO_LONG],
    [429, TTS_STATUS_MESSAGE.RATE_LIMITED],
    [502, TTS_STATUS_MESSAGE.UNAVAILABLE],
  ])("maps HTTP %i to its own status message", async (status, expectedMessage) => {
    const fetchImpl = vi.fn().mockResolvedValue(errorResponse(status));
    const { controller, status: statusEl } = makeHarness({ fetchImpl });

    await controller.handleClick("m1");

    expect(controller.getState("m1")).toBe(TTS_STATE.ERROR);
    expect(statusEl.textContent).toBe(expectedMessage);
  });

  it("fails cleanly (not stuck loading) when the <audio> element is missing", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const status = document.createElement("div");
    const controller = new TTSController({
      fetchImpl,
      getButton: () => makeButton(),
      getAudio: () => null,
      getStatus: () => status,
      errorResetDelayMs: 10,
    });

    await controller.handleClick("m1");

    expect(controller.getState("m1")).toBe(TTS_STATE.ERROR);
    expect(status.textContent).toBe(TTS_STATUS_MESSAGE.UNAVAILABLE);
  });

  it("catches a rejected audio.play() promise instead of leaving a stuck spinner", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller, status } = makeHarness({
      fetchImpl,
      playImpl: () => Promise.reject(new Error("autoplay blocked")),
    });

    await controller.handleClick("m1");

    expect(controller.getState("m1")).toBe(TTS_STATE.ERROR);
    expect(status.textContent).toBe(TTS_STATUS_MESSAGE.UNAVAILABLE);
  });

  it("returns to idle automatically after the error reset delay", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(errorResponse(404));
    let scheduled;
    const setTimeoutImpl = (callback, delay) => {
      scheduled = { callback, delay };
      return 0;
    };
    const { controller } = makeHarness({ fetchImpl, errorResetDelayMs: 10, setTimeoutImpl });

    await controller.handleClick("m1");
    expect(controller.getState("m1")).toBe(TTS_STATE.ERROR);

    expect(scheduled.delay).toBe(10);
    scheduled.callback(); // invoke the scheduled reset directly — no real wait

    expect(controller.getState("m1")).toBe(TTS_STATE.IDLE);
  });
});

describe("object URL cleanup", () => {
  it("revokeAll revokes every tracked object URL", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(blobResponse());
    const { controller } = makeHarness({ fetchImpl });

    await controller.handleClick("m1");
    controller.revokeAll();

    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");
  });
});

describe("initTTSController", () => {
  it("delegates clicks on .tts-toggle buttons within #chat-container", () => {
    document.body.innerHTML =
      '<div id="chat-container"><button class="tts-toggle" data-message-id="m1"></button></div>';
    const controller = { handleClick: vi.fn(), revokeAll: vi.fn() };

    initTTSController(controller);
    document.querySelector(".tts-toggle").click();

    expect(controller.handleClick).toHaveBeenCalledWith("m1");
  });

  it("ignores clicks on a disabled Listen button", () => {
    document.body.innerHTML =
      '<div id="chat-container"><button class="tts-toggle" data-message-id="m1" disabled></button></div>';
    const controller = { handleClick: vi.fn(), revokeAll: vi.fn() };

    initTTSController(controller);
    document.querySelector(".tts-toggle").click();

    expect(controller.handleClick).not.toHaveBeenCalled();
  });
});

describe("audio-mode auto-play on arrival", () => {
  function makeController() {
    return { handleClick: vi.fn(), play: vi.fn(), revokeAll: vi.fn() };
  }

  function afterSwap() {
    document.body.dispatchEvent(new Event(HTMX_AFTER_SWAP_EVENT, { bubbles: true }));
  }

  it("auto-plays the newest bot message when the checkbox is checked", () => {
    document.body.innerHTML = `
      <input type="checkbox" id="audio-mode-checkbox" checked>
      <div id="chat-container">
        <div class="message bot-message">
          <button class="tts-toggle" data-message-id="m1"></button>
        </div>
      </div>`;
    const controller = makeController();
    initTTSController(controller);

    afterSwap();

    expect(controller.play).toHaveBeenCalledWith("m1");
  });

  it("does not auto-play when the checkbox is unchecked", () => {
    document.body.innerHTML = `
      <input type="checkbox" id="audio-mode-checkbox">
      <div id="chat-container">
        <div class="message bot-message">
          <button class="tts-toggle" data-message-id="m1"></button>
        </div>
      </div>`;
    const controller = makeController();
    initTTSController(controller);

    afterSwap();

    expect(controller.play).not.toHaveBeenCalled();
  });

  it("does not auto-play a response whose Listen button is disabled (too long)", () => {
    document.body.innerHTML = `
      <input type="checkbox" id="audio-mode-checkbox" checked>
      <div id="chat-container">
        <div class="message bot-message">
          <button class="tts-toggle" data-message-id="m1" disabled></button>
        </div>
      </div>`;
    const controller = makeController();
    initTTSController(controller);

    afterSwap();

    expect(controller.play).not.toHaveBeenCalled();
  });

  it("does not auto-play when the latest child isn't a bot message", () => {
    document.body.innerHTML = `
      <input type="checkbox" id="audio-mode-checkbox" checked>
      <div id="chat-container">
        <div class="message user-message"><p>hi</p></div>
      </div>`;
    const controller = makeController();
    initTTSController(controller);

    afterSwap();

    expect(controller.play).not.toHaveBeenCalled();
  });

  it("re-checks the checkbox on every swap rather than caching its initial value", () => {
    document.body.innerHTML = `
      <input type="checkbox" id="audio-mode-checkbox">
      <div id="chat-container">
        <div class="message bot-message">
          <button class="tts-toggle" data-message-id="m1"></button>
        </div>
      </div>`;
    const controller = makeController();
    initTTSController(controller);

    afterSwap();
    expect(controller.play).not.toHaveBeenCalled();

    document.getElementById("audio-mode-checkbox").checked = true;
    afterSwap();
    expect(controller.play).toHaveBeenCalledWith("m1");
  });
});
