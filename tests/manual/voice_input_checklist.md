# Voice-input manual smoke test

The Web Speech API can't be reliably unit-tested without heavy
scaffolding — it requires real `getUserMedia` permission, a live audio
stream, and platform-specific speech-recognition models. Use this
checklist before every voice-related release. ~5 minutes per pass.

**Setup.** Launch the dev server (`./run-web.sh`) and open
`http://127.0.0.1:5005/wizard`. Pick the **Software Engineering** role,
set career start to a date 1-2 years ago, and click **Regenerate chunks**.
Click any chunk's **open** button so `#raw-notes` is the active textarea.

Walk through each browser you support.

## Chrome / Edge (Web Speech API → Google STT)

- [ ] Mic button is **visible** to the right of the chunk-nav row, not hidden.
- [ ] `aria-label="Start dictation"`, tooltip mentions ⇧⌘M shortcut.
- [ ] First click → **privacy modal** appears. It names *Google* and *Apple* as the speech vendors and offers *OK, got it* / *Not now*.
- [ ] Click *Not now* → modal closes; mic button stays idle; nothing recorded.
- [ ] Click again → modal does **not** reappear in this same session (post-OK). It also does not reappear after page reload — `rb.voicePrivacyAck` in `localStorage` persists.
- [ ] Press *OK, got it* → recording starts; the mic button flips to red and the icon pulses.
- [ ] Speak: *"shipped the dispatch service rewrite, peeing nine nine from four eighty milliseconds to ninety five."* Verify interim text appears in `#raw-notes` as you speak.
- [ ] On a ~1.5s pause, the final transcript commits — the chunk-row badge updates to *"X chars"* and the auto-save indicator briefly flashes *Saving… → Saved*.
- [ ] Press **⇧⌘M** (or ⇧⌃M on Windows/Linux) without leaving the textarea → dictation stops; button returns to idle.
- [ ] Press **⇧⌘M** again → dictation resumes; cursor remains in `#raw-notes`.
- [ ] Type a character into `#raw-notes` while recording → dictation pauses; existing committed text untouched.
- [ ] Leave dictation idle for >60s, then speak → engine restarts transparently and captures the new speech (browser closes the session at ~60s).
- [ ] **Mic permission denied** test: deny mic in browser settings, reload, click mic → status banner shows *"Microphone permission was denied…"* and the mic button enters the strikethrough error state.
- [ ] Refresh the page → notes the user dictated persist (auto-save fired).

## Safari

- [ ] Same flow as Chrome works; Safari uses Apple's STT.
- [ ] Privacy modal disclosure ("Apple's speech-recognition servers") is still accurate.
- [ ] **⇧⌘M** doesn't conflict with Safari's *Hide Other Applications* shortcut. (It only fires while focus is on a non-system area of the page.)

## Firefox (unsupported)

- [ ] Mic button is **hidden** entirely (`hidden` attribute set + `unsupported` class applied).
- [ ] Tooltip on the still-visible shortcut hint: *"Try Chrome, Edge, or Safari."* — verify via inspector that the button has `title="Voice typing isn't supported in this browser — try Chrome, Edge, or Safari."`
- [ ] No JS errors in the console.

## Languages

- [ ] Default language is **en-IN**. Confirm via DevTools:
      `state.voice.lang === "en-IN"`.
- [ ] Speech with an Indian English accent transcribes reasonably well.
- [ ] Code-mixed Hindi+English short phrases ("yeh kaam karta hai") transcribe partially — known limitation; document in README.

## Accessibility

- [ ] Keyboard-only: tab to the mic button, press Space/Enter → modal opens, OK button is focused inside the modal, ESC closes the modal.
- [ ] `#voice-live` aria-live region updates with interim transcript text (visible via DevTools → inspect element → live region polled at speech engine cadence).
- [ ] VoiceOver (Mac) announces state transitions ("Start dictation" / "Stop dictation").
