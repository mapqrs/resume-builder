# Resume Builder — Product Roadmap

Adoption- and UX-focused improvements, kept separate from `BOCK_ROADMAP.md`
(which tracks Laszlo Bock content/principles) and `HANDOFF.md` (session context).

Each item lists the problem it solves, the proposed change, the files to touch,
and a rough impact/effort read. Ordered by bang-for-buck. When an item ships,
move it to **Shipped** with a date and the files touched.

---

## ✅ Shipped

### 1. One-command, self-healing setup + auto-open browser
*Shipped 2026-07-11.*

The old `run-web.sh` errored out if the virtualenv didn't already exist, and the
README asked non-developers to run four terminal commands by hand — the wall most
non-technical users bounce off. Now:

- **`run-web.sh` self-heals.** On first run it finds a suitable Python (≥3.9,
  with platform-specific install hints if it's missing or too old), creates
  `.venv`, and installs dependencies. Later runs detect a healthy env and launch
  instantly. A `.venv/.deps-installed` stamp means we only re-install when
  `requirements.txt` actually changes. Optional-dependency failures (e.g. the
  Anthropic SDK) warn-and-continue instead of blocking launch.
- **The browser opens itself.** `web.main()` pops `http://127.0.0.1:5005` open
  ~1s after the server is ready. Opt out with `RESUME_BUILDER_NO_BROWSER=1`.
- **Python version guard.** Clear, platform-specific guidance instead of a stack
  trace when Python is too old or absent.

Files: `run-web.sh`, `src/resume_builder/web.py`.

Deferred follow-up: a double-clickable `.command` (macOS) / `.bat` (Windows)
wrapper so there's zero terminal at all.

### 4. In-UI "Try with sample data" + live AI-connection status
*Shipped 2026-07-11.*

New users no longer need a terminal `cp` to preview the tool, and the old top-bar
pill that *always* read "claude CLI" (regardless of what was actually available)
is gone. Now:

- **"Load sample data →" button** on the wizard copies
  `samples/master.example.yaml` into `master.yaml` (backing up any existing one,
  exactly like Save Master) via `POST /api/load-sample`, then drops the user
  straight into the tailor with the sample pre-filled.
- **Live AI-connection status**, computed once from `llm.provider_status()` and
  shown as a banner on both the wizard and home pages plus a dynamic top-bar
  pill: green *"Connected via Claude Code"* / *"Connected via API key"*, or amber
  *"Copy-paste mode — no AI connection found"* with how-to-fix guidance.

Files: `src/resume_builder/llm.py` (`provider_status()`), `web.py`
(`/api/load-sample` + index context), `wizard.py` (wizard context),
`templates/index.html`, `templates/wizard.html`, `static/style.css`,
`tests/test_onboarding.py` (7 new tests). Also set `RESUME_BUILDER_NO_BROWSER=1`
in `.claude/launch.json` so preview runs don't pop a second browser.

### 5. Extra-instructions box (tailor + cover letter)
*Shipped 2026-07-11.*

Freeform style/emphasis guidance — "emphasize leadership", "British English",
"more formal tone" — typed once in the Pointers card and applied to **both** the
resume tailor and the cover-letter writer, plus a `--extra-instructions` CLI
flag. Carried on the `Pointers` model (trimmed, capped at 2000 chars), rendered
into the prompt with an explicit fence: style only, the anti-fabrication HARD
RULES always win, and the text never extends the guard's legal vocabulary — so
an instruction can't smuggle a new number or tool name into the output.

Files: `schema.py` (Pointers.extra_instructions), `prompts.py`
(`_pointers_block`), `loaders.py` (merge), `web.py` (form parse), `cli.py`
(flag), `templates/index.html`, `tests/test_pointers.py` (6 new tests).

### 6. Save-as-defaults (pointers.yaml round-trip)
*Shipped 2026-07-11.*

A **Save as defaults** button in the Pointers card writes
length/seniority/context/must-include/extra-instructions to `pointers.yaml`;
the home page pre-fills all of them on every future visit (malformed files
fall back silently). The same file feeds the CLI's `--pointers`.

Files: `web.py` (`/api/save-defaults` + index pre-fill context),
`templates/index.html`, `tests/test_pointers.py` (4 new tests).

### 7. Template customizer (fonts / colors / margins / sections)
*Shipped 2026-07-11.*

A "Customize template" panel under the Format preset picker: font family
(applied across body/role/heading/name), per-role pt sizes, page size, accent
color, margins, line spacing, paragraph spacing, and a **section list you can
reorder and hide** (summary/experience/projects/education/skills). Seeds from
whichever preset is selected, saves to `template.yaml` (validated through the
Template schema before disk is touched), and auto-selects a new **"Custom ·
template.yaml"** preset option that the generate flow resolves server-side.
The same file drives the CLI's `--template template.yaml`.

Files: `web.py` (`/api/template-values`, `/api/save-template`, `custom` preset
resolution, `custom_exists` flag), `templates/index.html` (panel + JS),
`static/style.css`, `tests/test_template_customizer.py` (10 new tests).

### 8. Windows launcher
*Shipped 2026-07-11 — needs one verification run on a real Windows machine.*

`run-web.bat` mirrors `run-web.sh`: finds Python 3.9+ (via the `py` launcher
or PATH, with install guidance if absent), creates `.venv`, installs
dependencies with the same stamp-file skip logic and optional-dependency
tolerance, and launches. Double-clicking it in Explorer works.

Files: `run-web.bat`, `README.md` (quick start now covers Windows natively).

### 9. Résumé import as the wizard's front door
*Shipped 2026-07-12.*

The wizard's default path was a ~30-minute cold brain-dump even for people who
already had a résumé. Now **step 0** at the top of the wizard takes an upload
(PDF/DOCX/MD/TXT) or pasted text, makes one LLM parse call, and pre-fills the
whole session: one career chunk **per job** (labelled "Company — Role", seeded
with the résumé's own bullets as raw notes), employment metadata, basics,
education, summary, and one skills-bucket draft per skill. Every step below
becomes a review pass; the normal Extract → categorize → polish pipeline (and
the no-invention guard) is unchanged.

Details: parser prompt is transcribe-only (no embellishment; unparseable dates
become warnings, never guesses); warn-then-replace when the session already has
typed content (409 + force, same pattern as chunk extract); full copy-paste
fallback when no AI connection is available — unlike the older extract flow,
import works end-to-end without a key. The old per-chunk file import remains as
"append a file into this chunk".

Files: `src/resume_builder/resume_import.py` (new), `wizard.py`
(`/import-apply`, `/import-apply-response`), `templates/wizard.html`,
`static/wizard.js`, `static/wizard.css`, `tests/test_resume_import.py`.

### 10. Sessions gallery
*Shipped 2026-07-12.*

Closing the tab used to mean losing your `/wizard?session=<id>` URL — and with
it your work (`HANDOFF.md` §9 gap). The wizard now shows a collapsible **"Your
sessions"** bar listing every session newest-first with a human label (basics
name → newest employment → role family → id), relative last-edited time,
progress (chunks dumped / drafts / saved-to-master), plus **Continue** and
**Delete** (with confirm). Backed by `GET /api/wizard/sessions` and
`DELETE /api/wizard/<id>`.

Files: `wizard.py` (routes), `templates/wizard.html`, `static/wizard.js`,
`static/wizard.css`, `tests/test_resume_import.py` (gallery tests).

---

## 🎯 High impact, next up

### Applications tracker (sessions gallery, phase 2)
**Impact: medium-high · Effort: medium**

Extend the sessions gallery into a job-search companion: each generated résumé
with its JD, ATS score, and date, so the tool becomes something people return
to per application instead of a one-shot generator. Files: `web.py` (record
generations), a small on-disk index next to `out/`, `index.html`.

---

## 🧹 Smaller polish

- **Verify `run-web.bat` on a real Windows machine.** The logic mirrors the
  tested `run-web.sh`, but it has only been reviewed, not executed, on Windows.
- **Finish the reflect-synthesis copy-paste fallback.**
  `/api/reflect/synthesize` currently returns a 502 when neither `claude` nor
  `ANTHROPIC_API_KEY` is present (`HANDOFF.md` §9). Add the same copy-paste path
  the tailor and cover-letter flows already have. Files: `reflect_synth.py`,
  `web.py`, `templates/reflect.html`.
- **Surface optional-dependency state.** The status banner (#4) now shows which
  AI path is live. Remaining nuance: if `ANTHROPIC_API_KEY` is set but the
  `anthropic` SDK isn't installed, that path is silently unavailable — the banner
  could call that out specifically. Minor.
- **PDF without Word/LibreOffice.** Today the flow dead-ends with "open it and
  Save As PDF" if neither is installed. Either bundle a pure-Python fallback or
  make the inline guidance louder and earlier. Files: `pdf_export.py`,
  `index.html`.
- **Accessibility / mobile / responsive.** Deferred by design while this is a
  localhost tool (`HANDOFF.md`). Revisit only if it ever goes hosted.
- **Two-tabs / last-write-wins on a session.** Acceptable for localhost v1
  (`HANDOFF.md` §9); revisit alongside #3 if multi-session management lands.
- **`Master.schema_version` migration.** The hook is a comment in
  `loaders.py:load_master`; the first breaking schema change should ship
  `migrate_from_v1()`.

---

*Maintained alongside `HANDOFF.md`.*
