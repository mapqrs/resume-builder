"use strict";

/*
 * VoiceInput — attach Web Speech API dictation to any textarea.
 *
 *   const vi = new VoiceInput(textareaEl, { lang: "en-IN" }).attach({
 *     button: micButtonEl,   // required — toggle control
 *     status: ariaLiveEl,    // optional — interim transcript for screen readers
 *     onError: msg => ...,   // optional — user-facing error reporter
 *   });
 *
 * Streams interim transcripts directly into the textarea (firing the
 * native `input` event so any existing auto-save picks them up), appends
 * the final transcript on pause, and transparently restarts when the
 * browser closes the session after its ~60s idle window.
 *
 * The Whisper-WASM swap-in lands later under this same class signature —
 * see plan §Phase 1.5 + §Phase 10+.
 */

const RESTART_GRACE_MS = 100;   // small gap to let the engine settle
const MANUAL_EDIT_PAUSES = true; // user typing into the textarea stops dictation

class VoiceInput {
  /**
   * @param {HTMLTextAreaElement} textarea
   * @param {object} [opts]
   * @param {string} [opts.lang="en-IN"] — BCP-47 language tag
   */
  constructor(textarea, opts = {}) {
    this.textarea = textarea;
    this.lang = opts.lang || "en-IN";
    this.button = null;
    this.status = null;
    this.onError = null;

    this.recognition = null;
    this.isRecording = false;
    this.shouldRestart = false;      // true when end-event was not user-initiated
    this.committedValue = "";         // textarea value at the start of this turn
  }

  /** Detect whether the browser exposes SpeechRecognition at all. */
  static isAvailable() {
    return typeof window !== "undefined" &&
      ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);
  }

  /**
   * Wire up DOM events. Returns `this` so callers can chain.
   * @param {object} els
   * @param {HTMLElement} els.button
   * @param {HTMLElement} [els.status]
   * @param {(msg:string)=>void} [els.onError]
   */
  attach({ button, status, onError } = {}) {
    if (!button) throw new Error("VoiceInput.attach: button is required");
    this.button = button;
    this.status = status || null;
    this.onError = onError || (() => {});

    if (!VoiceInput.isAvailable()) {
      this._setState("unsupported");
      return this;
    }

    button.addEventListener("click", () => this.toggle());

    if (MANUAL_EDIT_PAUSES) {
      // If the user starts typing manually, stop dictation so they're not
      // fighting the engine for cursor position.
      this.textarea.addEventListener("keydown", (e) => {
        // Allow modifier-only or navigation keys; pause on real input.
        if (this.isRecording && _isContentKey(e)) this.stop();
      });
    }

    this._setState("idle");
    return this;
  }

  toggle() {
    if (this.isRecording) this.stop();
    else this.start();
  }

  start() {
    if (this.isRecording) return;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new Recognition();
    rec.lang = this.lang;
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      this.isRecording = true;
      this.shouldRestart = true;
      this.committedValue = this.textarea.value;
      this._setState("recording");
    };

    rec.onresult = (event) => this._handleResult(event);

    rec.onerror = (event) => {
      // `no-speech` is benign — engine just timed out waiting; let onend
      // restart us. Anything else is reportable.
      if (event.error && event.error !== "no-speech") {
        this.shouldRestart = false;
        this._setState("error");
        this.onError(_friendlyError(event.error));
      }
    };

    rec.onend = () => {
      this.isRecording = false;
      if (this.shouldRestart) {
        // Browser closed the session (60s idle window or "end of speech");
        // re-arm transparently.
        setTimeout(() => {
          if (this.shouldRestart) this.start();
        }, RESTART_GRACE_MS);
      } else if (this.button && !this.button.classList.contains("error")) {
        this._setState("idle");
      }
    };

    this.recognition = rec;
    try {
      rec.start();
    } catch (e) {
      // Mostly fires when start() is called twice in quick succession.
      this.shouldRestart = false;
      this._setState("error");
      this.onError(_friendlyError(String(e.message || e)));
    }
  }

  stop() {
    this.shouldRestart = false;
    if (this.recognition) {
      try { this.recognition.stop(); } catch (_) { /* already stopping */ }
    }
    this.isRecording = false;
    this._setState("idle");
    if (this.status) this.status.textContent = "";
  }

  // ---------- internals ----------

  _handleResult(event) {
    // Walk every result in the buffer. Final results get committed to the
    // textarea's value; interim ones are shown live but not yet committed.
    let finalChunk = "";
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      const text = r[0].transcript;
      if (r.isFinal) finalChunk += text;
      else interim += text;
    }

    if (finalChunk) {
      this.committedValue = _joinSentence(this.committedValue, finalChunk);
      this.textarea.value = this.committedValue;
      this.textarea.dispatchEvent(new Event("input", { bubbles: true }));
    } else if (interim) {
      // Show interim text live but don't commit; auto-save fires on the
      // next final result.
      this.textarea.value = _joinSentence(this.committedValue, interim);
    }
    if (this.status) this.status.textContent = (interim || finalChunk).trim();
  }

  _setState(state) {
    const b = this.button;
    if (!b) return;
    b.classList.remove("recording", "error", "unsupported");
    if (state === "unsupported") {
      b.classList.add("unsupported");
      b.disabled = true;
      b.setAttribute("aria-label", "Voice typing not supported in this browser");
      b.title = "Voice typing isn't supported in this browser — try Chrome, Edge, or Safari.";
      b.hidden = true;
      return;
    }
    if (state === "recording") {
      b.classList.add("recording");
      b.setAttribute("aria-label", "Stop dictation");
      b.title = "Click to stop dictation (⇧⌘M).";
    } else if (state === "error") {
      b.classList.add("error");
      b.setAttribute("aria-label", "Voice typing failed — click to retry");
      b.title = "Voice typing failed. Click to retry.";
    } else {
      b.setAttribute("aria-label", "Start dictation");
      b.title = "Click to dictate (⇧⌘M).";
    }
  }
}

// ---------- helpers ----------

function _joinSentence(prefix, addition) {
  const left = (prefix || "").replace(/\s+$/, "");
  const right = (addition || "").replace(/^\s+/, "");
  if (!left) return right;
  if (!right) return left;
  // Insert a space when the previous chunk doesn't already end in punctuation
  // or whitespace. Avoids "shipped Xdictated more" runs.
  const lastChar = left.slice(-1);
  const joiner = /[\s.,;:!?]/.test(lastChar) ? " " : " ";
  return left + joiner + right;
}

function _isContentKey(e) {
  // Treat character keys + common edit keys as "real input." Skip modifiers
  // alone, arrow keys, escape, tab, etc.
  if (e.metaKey || e.ctrlKey || e.altKey) return false;
  const k = e.key;
  if (!k) return false;
  if (k.length === 1) return true;       // printable
  return k === "Backspace" || k === "Delete" || k === "Enter";
}

function _friendlyError(code) {
  switch (code) {
    case "audio-capture":
      return "No microphone detected. Plug one in and try again.";
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone permission was denied. Allow it in your browser settings.";
    case "network":
      return "Speech-recognition service is unreachable. Check your network.";
    case "aborted":
      return "Dictation stopped.";
    case "language-not-supported":
      return "The selected language isn't supported by your browser.";
    default:
      return `Voice typing error: ${code}`;
  }
}

// ESM-friendly export for tests; browser globals stay populated too.
if (typeof module !== "undefined" && module.exports) module.exports = { VoiceInput };
if (typeof window !== "undefined") window.VoiceInput = VoiceInput;
