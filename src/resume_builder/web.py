"""Local Flask UI for the resume builder.

Bind: 127.0.0.1 only. No auth, no CSRF — this is a personal localhost tool.
Run: `python -m resume_builder.web` (defaults to http://127.0.0.1:5005).

Override with env: RESUME_BUILDER_HOST, RESUME_BUILDER_PORT.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import applications
from .ats import score_ats
from .cover_letter import (
    CoverLetterWarning,
    cover_letter_auto,
    cover_letter_to_plain_text,
    cover_letter_via_claude_cli,
    parse_cover_letter_response_text,
    validate_cover_letter,
)
from .diff import build_diff
from .file_extract import ExtractError, extract_text
from .formatting_check import check_template
from .guard import GuardWarning, validate
from .jd_signals import (
    JDSignals,
    extract as extract_signals,
    from_target_role,
    target_role_to_jd_text,
)
from .lints import lint
from .llm import CopyPasteRequired, pick_provider, provider_status
from .loaders import apply_auto_length, load_pointers, pointers_from_dict
from .pdf_export import (
    ConversionError,
    LastResortError,
    available_converter_name,
    convert_docx_to_pdf,
)
from .prompts import (
    COVER_LETTER_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_cover_letter_user_message,
    build_user_message,
)
from .render import render_cover_letter_docx, render_docx
from .schema import Master, Pointers, TailoredResume, TargetRole, Template
from .template_presets import (
    PRESETS as TEMPLATE_PRESETS,
    all_presets_for_ui,
    default_preset_id,
    get_preset,
)
from .tailor import (
    ClaudeCliError,
    parse_response_text,
    tailor_auto,
    tailor_via_claude_cli,
)
from .wizard import wizard_bp


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"


app = Flask(__name__)
# 6 MB — enough for a multi-page PDF JD; rejects pathological uploads.
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
app.register_blueprint(wizard_bp)


# ----- result cache for the diff view -----
#
# After each successful generate, stash master + cleaned tailored + warnings
# under a short uuid. /api/diff/<id> reads it back and joins for the UI.
# Bounded by count and TTL — this is a localhost personal tool.

_RECENT_RESULTS: dict[str, dict] = {}
_MAX_RECENT = 10
_RECENT_TTL_S = 600


def _evict_recent() -> None:
    now = time.time()
    for k in [k for k, v in _RECENT_RESULTS.items() if now - v["ts"] > _RECENT_TTL_S]:
        _RECENT_RESULTS.pop(k, None)
    while len(_RECENT_RESULTS) >= _MAX_RECENT:
        oldest = min(_RECENT_RESULTS, key=lambda k: _RECENT_RESULTS[k]["ts"])
        _RECENT_RESULTS.pop(oldest, None)


def _store_result(
    master: Master,
    tailored: TailoredResume,
    jd_text: str,
    guard_warnings: list[GuardWarning],
) -> str:
    _evict_recent()
    rid = uuid.uuid4().hex[:12]
    _RECENT_RESULTS[rid] = {
        "master": master,
        "tailored": tailored,
        "jd_text": jd_text,
        "guard_warnings": guard_warnings,
        "ts": time.time(),
    }
    return rid


def _parse_master_yaml(text: str) -> Master:
    return Master.model_validate(yaml.safe_load(text))


def _parse_template_yaml(text: str | None) -> Template:
    if not text or not text.strip():
        return Template()
    return Template.model_validate(yaml.safe_load(text))


def _custom_template_path() -> Path:
    return Path.cwd() / "template.yaml"


def _load_custom_template() -> Template:
    """The user's saved ``template.yaml`` (see the template customizer),
    falling back to defaults when the file is absent or malformed."""
    path = _custom_template_path()
    if not path.is_file():
        return Template()
    try:
        return Template.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except Exception:
        return Template()


def _resolve_template_and_length(form: Any) -> tuple[Template, str | None]:
    """Resolve the template + an optional length pointer override.

    Form precedence (highest first):
      1. ``preset`` — named preset (Phase 10 item 1), or the sentinel
         ``custom`` which loads the user's saved ``template.yaml``
         (written by the template customizer). Named presets also seed
         the length pointer when the form didn't set one.
      2. ``template_yaml`` — raw YAML; used as the editable override.
      3. Defaults (``Template()``).
    """
    preset_id = (form.get("preset") or "").strip()
    if preset_id == "custom":
        return _load_custom_template(), None
    if preset_id:
        try:
            preset = get_preset(preset_id)
        except KeyError as e:
            raise ValueError(str(e)) from e
        # Length pointer override: only fill when the form didn't already set one.
        length_override = preset.length_pointer if not form.get("length") else None
        return preset.template, length_override
    return _parse_template_yaml(form.get("template_yaml")), None


def _pointers_from_form(form: Any) -> Pointers:
    return pointers_from_dict(
        Pointers(),
        {
            "length": form.get("length") or None,
            "seniority": form.get("seniority") or None,
            "context": form.get("context") or None,
            "must_include": form.get("must_include") or None,
            "extra_instructions": form.get("extra_instructions") or None,
        },
    )


def _parse_target_role(form: Any) -> TargetRole | None:
    """If the form carries ``target_role_json``, parse it into a TargetRole.

    Used by the JD-less mode (Phase 7) — when the user has no JD on hand,
    they fill the target-role form and the tailor synthesizes JDSignals from
    that. Returns ``None`` when the field is absent or empty.
    """
    raw = form.get("target_role_json", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"target_role_json is not valid JSON: {e}") from e
    return TargetRole.model_validate(data)


def _required_inputs(form: Any) -> tuple[Master, Template, Pointers, str, TargetRole | None]:
    """Validate the four common inputs plus the optional target_role.

    Returns ``(master, template, pointers, jd_text, target_role)``. When the
    user supplied a target_role but no jd_text, ``jd_text`` here is whatever
    the user typed (possibly empty); callers should synthesize a JD blob
    from ``target_role`` via ``target_role_to_jd_text()`` before downstream
    consumption.

    The ``preset`` form field, if set, picks a named template preset that
    overrides ``template_yaml`` and may seed the ``length`` pointer.

    Raises ``ValueError`` on bad input.
    """
    master_yaml = form.get("master_yaml", "").strip()
    if not master_yaml:
        raise ValueError("master_yaml is required")
    master = _parse_master_yaml(master_yaml)
    template, length_override = _resolve_template_and_length(form)
    pointers = _pointers_from_form(form)
    if length_override and not pointers.length:
        pointers.length = length_override
    pointers = apply_auto_length(master, pointers)
    jd_text = form.get("jd_text", "").strip()
    target_role = _parse_target_role(form)
    return master, template, pointers, jd_text, target_role


def _jd_and_signals_for_input(
    jd_text: str, target_role: TargetRole | None,
) -> tuple[str, JDSignals]:
    """Resolve the (jd_text, signals) pair handed to the tailor.

    Three cases:
    1. JD text present → use it verbatim, extract signals heuristically.
    2. JD text absent, target_role present → synthesize both from the target.
    3. Neither → raise (caller should already have rejected).
    """
    if jd_text:
        return jd_text, extract_signals(jd_text)
    if target_role is not None:
        return target_role_to_jd_text(target_role), from_target_role(target_role)
    raise ValueError("either jd_text or target_role_json is required")


def _applications_path() -> Path:
    return Path(app.config.get("APPLICATIONS_PATH", Path.cwd() / "applications.json"))


def _docx_response(
    master: Master,
    template: Template,
    tailored,
    download_name: str,
    pointers: Pointers | None = None,
    guard_warnings: list[GuardWarning] | None = None,
    signals: JDSignals | None = None,
    jd_text: str | None = None,
    from_target_role: bool = False,
):
    """Render the docx, stream it, attach guard + lint + JD signal data as headers,
    and stash the result in the cache so /api/diff can read it back.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".docx", prefix="resume-")
    os.close(fd)
    try:
        render_docx(master, template, tmp_path, tailored=tailored)
        with open(tmp_path, "rb") as f:
            payload = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    resp = send_file(
        io.BytesIO(payload),
        as_attachment=True,
        download_name=download_name,
        mimetype=DOCX_MIME,
    )
    exposed: list[str] = []
    if guard_warnings:
        warnings_json = [
            {
                "source_id": w.bullet_source_id,
                "reason": w.reason,
                "rewritten": w.rewritten_text,
            }
            for w in guard_warnings
        ]
        resp.headers["X-Guard-Warnings"] = json.dumps(warnings_json)[:8192]
        exposed.append("X-Guard-Warnings")
    if tailored is not None:
        lints_out = lint(tailored, pointers=pointers, master=master)
        if lints_out:
            payload_lints = [
                {
                    "rule": w.rule,
                    "message": w.message,
                    "source_id": w.source_id,
                    "snippet": w.snippet,
                    "suggestion": w.suggestion,
                }
                for w in lints_out
            ]
            resp.headers["X-Lint-Warnings"] = json.dumps(payload_lints)[:8192]
            exposed.append("X-Lint-Warnings")
    if signals is not None:
        resp.headers["X-JD-Signals"] = json.dumps(signals.for_prompt())[:8192]
        exposed.append("X-JD-Signals")
    formatting_warnings = check_template(template)
    if formatting_warnings:
        resp.headers["X-Bock-Formatting"] = json.dumps(
            [w.__dict__ for w in formatting_warnings]
        )[:8192]
        exposed.append("X-Bock-Formatting")
    # ATS report: extract plain text from the rendered docx (what an ATS parser
    # would see) and score it against the JD's keyword set.
    ats_report = None
    if tailored is not None and (signals is not None or (pointers and pointers.must_include)):
        try:
            docx_text, _ = extract_text("rendered.docx", payload)
            ats_report = score_ats(docx_text, signals=signals, pointers=pointers)
            resp.headers["X-ATS-Report"] = json.dumps(ats_report.model_dump())[:8192]
            exposed.append("X-ATS-Report")
        except Exception:  # noqa: BLE001 — ATS is advisory; never fail the response
            ats_report = None
    if tailored is not None and jd_text is not None:
        rid = _store_result(master, tailored, jd_text, guard_warnings or [])
        resp.headers["X-Result-Id"] = rid
        exposed.append("X-Result-Id")
        # Log the application (best-effort — a logging failure must never break
        # the download the user is waiting on).
        try:
            applications.record(
                _applications_path(),
                signals=signals,
                pointers=pointers,
                jd_text=jd_text,
                ats_report=ats_report,
                from_target_role=from_target_role,
                guard_dropped=len(guard_warnings or []),
            )
        except Exception:  # noqa: BLE001
            pass
    if exposed:
        resp.headers["Access-Control-Expose-Headers"] = ", ".join(exposed)
    return resp


@app.route("/", methods=["GET"])
def index():
    """Home page. First-run behavior (Phase 9): when no ``master.yaml``
    exists in the working directory, redirect to ``/wizard`` so a fresh
    clone lands directly on the brain-dump flow.

    Power-user override: ``?skip-wizard=1`` or a returning user with
    ``?skip-wizard=1`` cookied skips the redirect. Tests can also disable
    via ``app.config["DISABLE_FIRSTRUN_REDIRECT"] = True``.
    """
    cwd = Path.cwd()
    master_default = ""
    candidate = cwd / "master.yaml"
    has_master = candidate.exists() and candidate.is_file()
    if has_master:
        try:
            text = candidate.read_text(encoding="utf-8")
            if len(text) <= 256 * 1024:  # don't pre-fill if absurdly large
                master_default = text
        except OSError:
            pass

    skip_redirect = (
        request.args.get("skip-wizard") in ("1", "true", "yes")
        or app.config.get("DISABLE_FIRSTRUN_REDIRECT")
    )
    if not has_master and not skip_redirect:
        return redirect(url_for("wizard.wizard_page"))

    # Saved defaults (pointers.yaml) pre-fill the Pointers card. A malformed
    # file must never take down the home page — fall back to no defaults.
    pointers_default: dict[str, Any] = {}
    ppath = cwd / "pointers.yaml"
    if ppath.is_file():
        try:
            pointers_default = load_pointers(ppath).model_dump(exclude_none=True)
        except Exception:
            pointers_default = {}

    return render_template(
        "index.html",
        master_default=master_default,
        provider_status=provider_status(),
        pointers_default=pointers_default,
    )


@app.route("/api/save-defaults", methods=["POST"])
def save_defaults():
    """Persist the Pointers-card values as ``pointers.yaml`` so they become
    the pre-filled defaults for future sessions (web) and ``--pointers``
    runs (CLI). Plain overwrite — it's a tiny regenerable knobs file.
    """
    try:
        pointers = _pointers_from_form(request.form)
    except Exception as e:  # pydantic ValidationError on bad enum values etc.
        return jsonify({"error": "invalid_pointers", "detail": str(e)}), 400

    path = Path.cwd() / "pointers.yaml"
    header = (
        "# Default per-run pointers. Saved from the web UI "
        "(Pointers → Save as defaults).\n"
        "# Override any of these with CLI flags.\n"
    )
    path.write_text(
        header + yaml.safe_dump(
            pointers.model_dump(exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return jsonify({"ok": True, "saved_path": str(path)})


@app.route("/api/load-sample", methods=["POST"])
def load_sample():
    """Copy the bundled example master (``samples/master.example.yaml``) into
    ``master.yaml`` so a new user can see the whole pipeline without typing a
    resume first. Any existing ``master.yaml`` is backed up to
    ``master.yaml.bak.<UTC>`` — never clobbered — matching Save Master.
    """
    repo_root = Path(__file__).resolve().parents[2]
    sample = repo_root / "samples" / "master.example.yaml"
    if not sample.is_file():
        return jsonify({
            "error": "sample_missing",
            "detail": "samples/master.example.yaml was not found.",
        }), 404

    master_path = Path.cwd() / "master.yaml"
    backup_path = None
    if master_path.exists():
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_path = master_path.with_name(f"master.yaml.bak.{stamp}")
        master_path.replace(backup_path)

    master_path.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")

    return jsonify({
        "ok": True,
        "saved_path": str(master_path),
        "backup_path": str(backup_path) if backup_path else None,
    })


@app.route("/api/applications", methods=["GET"])
def api_applications():
    """List past résumé generations, newest first (the applications tracker)."""
    apps = applications.load(_applications_path())
    return jsonify({"applications": [a.model_dump() for a in apps]})


@app.route("/api/applications/<app_id>", methods=["PATCH"])
def api_update_application(app_id: str):
    """Update an application's pipeline status (saved/applied/…/rejected)."""
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in applications.STATUSES:
        return jsonify({
            "error": "invalid_status",
            "valid": list(applications.STATUSES),
        }), 400
    updated = applications.update_status(app_id, status, _applications_path())
    if updated is None:
        return jsonify({"error": "not_found", "id": app_id}), 404
    return jsonify({"ok": True, "application": updated.model_dump()})


@app.route("/api/applications/<app_id>", methods=["DELETE"])
def api_delete_application(app_id: str):
    """Remove one application record. 404 when the id isn't found."""
    if not applications.delete(app_id, _applications_path()):
        return jsonify({"error": "not_found", "id": app_id}), 404
    return jsonify({"ok": True, "deleted": app_id})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    """Tailor via `claude -p` and return the rendered .docx.

    Accepts either ``jd_text`` (the usual path) or ``target_role_json``
    (Phase 7 JD-less mode). When the JD is absent, the target-role payload
    is synthesized into a JD-like brief + signals via ``from_target_role``.
    """
    try:
        master, template, pointers, jd_text, target_role = _required_inputs(request.form)
    except (ValueError, yaml.YAMLError) as e:
        return jsonify({"error": f"input parse error: {e}"}), 400

    if not jd_text and target_role is None:
        return jsonify({
            "error": "jd_text or target_role_json is required",
            "hint": "Paste a JD, or fill the target-role form (no-JD mode).",
        }), 400

    used_target_role = not jd_text and target_role is not None
    try:
        jd_text, signals = _jd_and_signals_for_input(jd_text, target_role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    model = request.form.get("model", "sonnet")
    try:
        # Auto-pick: claude -p → ANTHROPIC_API_KEY → copy-paste fallback.
        raw, _provider_reason = tailor_auto(
            master, jd_text, pointers, model=model, signals=signals,
        )
    except CopyPasteRequired:
        return (
            jsonify({
                "error": (
                    "No automated AI access available "
                    "(no Claude Code login, no ANTHROPIC_API_KEY)."
                ),
                "hint": (
                    "Use the Copy-paste tab: Show prompt → paste into any "
                    "Claude session → paste reply back → Render."
                ),
            }),
            502,
        )
    except ClaudeCliError as e:
        return (
            jsonify({
                "error": str(e),
                "hint": (
                    "Set ANTHROPIC_API_KEY for the API path, log in to Claude "
                    "Code for the subprocess path, or use the Copy-paste tab."
                ),
            }),
            502,
        )
    except Exception as e:  # noqa: BLE001 — surface unexpected errors
        return jsonify({"error": f"tailor failed: {e}"}), 500

    guard = validate(master, raw, jd_text, pointers=pointers)
    return _docx_response(
        master, template, guard.cleaned, "resume.docx",
        pointers=pointers, guard_warnings=guard.warnings,
        signals=signals, jd_text=jd_text, from_target_role=used_target_role,
    )


@app.route("/api/prompt", methods=["POST"])
def api_prompt():
    """Return the full prompt as text/plain so the user can paste it elsewhere."""
    try:
        master, _template, pointers, jd_text, target_role = _required_inputs(request.form)
    except (ValueError, yaml.YAMLError) as e:
        return jsonify({"error": f"input parse error: {e}"}), 400

    if not jd_text and target_role is None:
        return jsonify({"error": "jd_text or target_role_json is required"}), 400

    try:
        jd_text, signals = _jd_and_signals_for_input(jd_text, target_role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    body = (
        "# === SYSTEM ===\n"
        + SYSTEM_PROMPT
        + "\n\n# === USER MESSAGE ===\n"
        + build_user_message(master, jd_text, pointers, signals=signals)
        + "\n"
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/api/from-response", methods=["POST"])
def api_from_response():
    """Re-render from a previously-collected Claude response (paste mode step 2)."""
    try:
        master, template, pointers, jd_text, target_role = _required_inputs(request.form)
    except (ValueError, yaml.YAMLError) as e:
        return jsonify({"error": f"input parse error: {e}"}), 400

    response_text = request.form.get("response_text", "").strip()
    if not response_text:
        return jsonify({"error": "response_text is required"}), 400
    if not jd_text and target_role is None:
        return jsonify({
            "error": "jd_text or target_role_json is required (used for guard validation)",
        }), 400

    used_target_role = not jd_text and target_role is not None
    try:
        jd_text, signals = _jd_and_signals_for_input(jd_text, target_role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        raw = parse_response_text(response_text)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"could not parse Claude response: {e}"}), 400

    guard = validate(master, raw, jd_text, pointers=pointers)
    return _docx_response(
        master, template, guard.cleaned, "resume.docx",
        pointers=pointers, guard_warnings=guard.warnings,
        signals=signals, jd_text=jd_text, from_target_role=used_target_role,
    )


@app.route("/api/cover-generate", methods=["POST"])
def api_cover_generate():
    """Generate a cover letter via `claude -p` and return a .docx."""
    try:
        master, template, pointers, jd_text, target_role = _required_inputs(request.form)
    except (ValueError, yaml.YAMLError) as e:
        return jsonify({"error": f"input parse error: {e}"}), 400
    if not jd_text and target_role is None:
        return jsonify({"error": "jd_text or target_role_json is required"}), 400

    try:
        jd_text, signals = _jd_and_signals_for_input(jd_text, target_role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    model = request.form.get("model", "sonnet")
    try:
        cover, _provider_reason = cover_letter_auto(
            master, jd_text, pointers, model=model, signals=signals,
        )
    except CopyPasteRequired:
        return (
            jsonify({
                "error": (
                    "No automated AI access available "
                    "(no Claude Code login, no ANTHROPIC_API_KEY)."
                ),
                "hint": (
                    "Use the Copy-paste tab: Show cover prompt → paste into "
                    "any Claude session → paste reply back → Render cover letter."
                ),
            }),
            502,
        )
    except ClaudeCliError as e:
        return (
            jsonify(
                {
                    "error": str(e),
                    "hint": (
                        "Set ANTHROPIC_API_KEY, log in to Claude Code, "
                        "or use the Copy-paste tab: Show cover prompt → paste into "
                        "any Claude session → paste reply back → Render cover letter."
                    ),
                }
            ),
            502,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"cover letter generation failed: {e}"}), 500

    return _cover_response(master, template, cover, jd_text, pointers)


@app.route("/api/cover-prompt", methods=["POST"])
def api_cover_prompt():
    """Return the cover-letter prompt as text/plain for paste mode."""
    try:
        master, _template, pointers, jd_text, target_role = _required_inputs(request.form)
    except (ValueError, yaml.YAMLError) as e:
        return jsonify({"error": f"input parse error: {e}"}), 400
    if not jd_text and target_role is None:
        return jsonify({"error": "jd_text or target_role_json is required"}), 400

    try:
        jd_text, signals = _jd_and_signals_for_input(jd_text, target_role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    body = (
        "# === SYSTEM ===\n"
        + COVER_LETTER_SYSTEM_PROMPT
        + "\n\n# === USER MESSAGE ===\n"
        + build_cover_letter_user_message(master, jd_text, pointers, signals=signals)
        + "\n"
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/api/cover-from-response", methods=["POST"])
def api_cover_from_response():
    """Re-render a cover letter from a previously-collected Claude response."""
    try:
        master, template, pointers, jd_text, target_role = _required_inputs(request.form)
    except (ValueError, yaml.YAMLError) as e:
        return jsonify({"error": f"input parse error: {e}"}), 400

    response_text = request.form.get("response_text", "").strip()
    if not response_text:
        return jsonify({"error": "response_text is required"}), 400
    if not jd_text and target_role is None:
        return jsonify({
            "error": "jd_text or target_role_json is required (used for guard validation)",
        }), 400

    try:
        jd_text, _signals = _jd_and_signals_for_input(jd_text, target_role)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        cover = parse_cover_letter_response_text(response_text)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"could not parse Claude response: {e}"}), 400

    return _cover_response(master, template, cover, jd_text, pointers)


def _cover_response(master, template, cover, jd_text, pointers):
    """Run the cover-letter guard, render the .docx, return with warnings header."""
    guard = validate_cover_letter(master, cover, jd_text, pointers=pointers)

    fd, tmp_path = tempfile.mkstemp(suffix=".docx", prefix="cover-")
    os.close(fd)
    try:
        render_cover_letter_docx(master, template, cover, tmp_path)
        with open(tmp_path, "rb") as f:
            payload = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    resp = send_file(
        io.BytesIO(payload),
        as_attachment=True,
        download_name="cover-letter.docx",
        mimetype=DOCX_MIME,
    )
    exposed: list[str] = []
    if guard.warnings:
        warns_json = [
            {
                "paragraph_index": w.paragraph_index,
                "paragraph_role": w.paragraph_role,
                "reason": w.reason,
                "text": w.text,
            }
            for w in guard.warnings
        ]
        resp.headers["X-Cover-Warnings"] = json.dumps(warns_json)[:8192]
        exposed.append("X-Cover-Warnings")
    # Plain-text version of the cover letter, header-delivered so the UI can
    # render it inline alongside the download.
    try:
        plain = cover_letter_to_plain_text(cover, master)
        # Plain text might be larger than 8KB; truncate the header version and
        # include a flag if so.
        encoded = plain.replace("\r", "").replace("\n", "\\n")
        if len(encoded) > 8000:
            encoded = encoded[:8000] + "...[truncated]"
        resp.headers["X-Cover-Text"] = encoded
        exposed.append("X-Cover-Text")
    except Exception:  # noqa: BLE001
        pass
    if exposed:
        resp.headers["Access-Control-Expose-Headers"] = ", ".join(exposed)
    return resp


@app.route("/api/diff/<result_id>", methods=["GET"])
def api_diff(result_id: str):
    """Return the master ↔ tailored diff payload for a recent generate."""
    entry = _RECENT_RESULTS.get(result_id)
    if entry is None:
        return jsonify({"error": "unknown or expired result id"}), 404
    diff = build_diff(
        entry["master"], entry["tailored"], guard_warnings=entry["guard_warnings"]
    )
    return jsonify(diff.model_dump())


@app.route("/about", methods=["GET"])
def about():
    """Intro / about page (Phase 10 item 3). Static template; no data."""
    return render_template("about.html")


@app.route("/reflect", methods=["GET"])
def reflect():
    """Self-reflection flow for finding the next job (Phase 10 item 4).

    Standalone from the wizard. Walks the user through the "Four Levers
    of Your Edge" worksheet; the in-browser localStorage layer persists
    the answers; ``/api/reflect/synthesize`` patterns them into a 1-page
    summary when the user clicks Synthesize.
    """
    return render_template("reflect.html")


@app.route("/api/reflect/synthesize", methods=["POST"])
def api_reflect_synthesize():
    """Pattern the user's worksheet answers into a 1-page summary.

    Request body: ``{"answers": {"l1-p1": "...", ...}}`` — every key from
    ``reflect_synth.LEVER_KEYS`` is accepted; missing or blank values are
    fine (the synthesis surfaces sparse sections honestly).

    Response: ``{"edge_summary", "next_steps", "master_additions",
    "linkedin_additions", "rationale", "warnings", "provider"}``.
    """
    from .reflect_synth import (
        LEVER_KEYS,
        MIN_FILLED_ANSWERS,
        ReflectSynthError,
        filled_answer_count,
        synthesize as reflect_synthesize,
    )

    payload = request.get_json(silent=True) or {}
    answers = payload.get("answers") or {}
    if not isinstance(answers, dict):
        return jsonify({"error": "`answers` must be an object"}), 400

    # Filter to known keys + coerce to strings so the LLM never sees junk.
    valid_keys = {k for keys in LEVER_KEYS.values() for k in keys}
    filtered = {
        k: (str(v) if v is not None else "")
        for k, v in answers.items()
        if k in valid_keys
    }

    if filled_answer_count(filtered) < MIN_FILLED_ANSWERS:
        return jsonify({
            "error": "insufficient_answers",
            "min_filled": MIN_FILLED_ANSWERS,
            "filled": filled_answer_count(filtered),
            "hint": (
                f"Fill in at least {MIN_FILLED_ANSWERS} non-blank answers "
                "before synthesizing — the LLM needs material to pattern."
            ),
        }), 400

    try:
        choice = pick_provider()
    except CopyPasteRequired:  # pragma: no cover — pick_provider returns
        choice = None

    if choice is None or choice.provider.name == "copy-paste":
        return jsonify({
            "error": "copy_paste_required",
            "hint": (
                "No automated AI access available. Log in to Claude Code "
                "or set ANTHROPIC_API_KEY. Copy-paste flow for the reflect "
                "synthesis is on the roadmap."
            ),
        }), 502

    try:
        result, guard, user_msg, raw_response = reflect_synthesize(
            filtered, choice.provider,
        )
    except CopyPasteRequired:
        return jsonify({
            "error": "copy_paste_required",
            "hint": (
                "No automated AI access available. Log in to Claude Code "
                "or set ANTHROPIC_API_KEY."
            ),
        }), 502
    except ReflectSynthError as e:
        return jsonify({"error": "synthesis_failed", "detail": str(e)}), 502
    except Exception as e:  # noqa: BLE001 — surface unexpected errors
        return jsonify({"error": "synthesis_failed", "detail": str(e)}), 500

    return jsonify({
        "edge_summary": result.edge_summary,
        "next_steps": result.next_steps,
        "master_additions": result.master_additions,
        "linkedin_additions": result.linkedin_additions,
        "rationale": result.rationale,
        "warnings": [
            {"section": w.section, "reason": w.reason, "text": w.text}
            for w in guard.warnings
        ],
        "provider": {"name": choice.provider.name, "reason": choice.reason},
    })


@app.route("/api/template-presets", methods=["GET"])
def api_template_presets():
    """List the named format presets (Phase 10 item 1) the UI can pick from.

    Each entry carries id, label, length_pointer hint, and the guidance copy
    (best_for / pages / years_experience / notes). The default preset is
    flagged so the UI can pre-select it.
    """
    return jsonify({
        "presets": all_presets_for_ui(),
        "default_id": default_preset_id(),
        # Whether a saved template.yaml exists — the UI adds a
        # "Custom · template.yaml" option to the picker when it does.
        "custom_exists": _custom_template_path().is_file(),
    })


@app.route("/api/template-values", methods=["GET"])
def api_template_values():
    """Resolved Template values as JSON — seeds the customizer panel.

    ``?preset=<id>`` returns that preset's values; ``?preset=custom`` (or no
    param) returns the saved ``template.yaml`` (defaults when absent).
    """
    preset_id = (request.args.get("preset") or "custom").strip()
    if preset_id and preset_id != "custom":
        try:
            template = get_preset(preset_id).template
        except KeyError as e:
            return jsonify({"error": str(e)}), 404
    else:
        template = _load_custom_template()
    return jsonify({"template": template.model_dump(), "preset": preset_id})


@app.route("/api/save-template", methods=["POST"])
def api_save_template():
    """Persist customizer values as ``template.yaml``.

    Body: JSON ``{"template": {...}}`` matching the Template schema. The
    payload is validated before disk is touched; the saved file is what the
    ``custom`` preset and the CLI's ``--template template.yaml`` load.
    """
    payload = request.get_json(silent=True) or {}
    data = payload.get("template")
    if not isinstance(data, dict):
        return jsonify({"error": "body must be JSON: {\"template\": {...}}"}), 400
    try:
        template = Template.model_validate(data)
    except Exception as e:
        return jsonify({"error": "invalid_template", "detail": str(e)}), 400

    header = (
        "# Resume formatting template. Saved from the web UI "
        "(Format preset → Customize).\n"
        "# Used by the \"Custom\" preset in the web UI and by "
        "`--template template.yaml` on the CLI.\n"
    )
    path = _custom_template_path()
    path.write_text(
        header + yaml.safe_dump(
            template.model_dump(), sort_keys=False, allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return jsonify({"ok": True, "saved_path": str(path)})


@app.route("/api/pdf-converter", methods=["GET"])
def api_pdf_converter():
    """Report which PDF converter (if any) is available on this machine.

    Lets the UI grey out the "Download PDF" button — or surface the
    "open in Word, File → Save As → PDF" hint — before the user clicks.
    """
    name = available_converter_name()
    return jsonify({
        "converter": name,
        "available": name is not None,
        "hint": (
            "Install Microsoft Word (macOS/Windows) or LibreOffice "
            "(cross-platform) to enable Download PDF, or open the .docx "
            "and File → Save As → PDF manually."
            if name is None else None
        ),
    })


@app.route("/api/docx-to-pdf", methods=["POST"])
def api_docx_to_pdf():
    """Accept an uploaded .docx and stream back the converted .pdf.

    The UI calls /api/generate first (which produces a .docx and returns
    its bytes), then re-uploads those bytes here to get a PDF. Round-trip
    means we don't need to keep the .docx in server memory between
    requests, and the conversion never blocks the original generation.
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "file is required"}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "file is empty"}), 400

    base = Path(f.filename).stem or "resume"
    fd_docx, docx_path = tempfile.mkstemp(suffix=".docx", prefix=f"{base}-")
    os.close(fd_docx)
    fd_pdf, pdf_path = tempfile.mkstemp(suffix=".pdf", prefix=f"{base}-")
    os.close(fd_pdf)

    try:
        Path(docx_path).write_bytes(data)
        try:
            result = convert_docx_to_pdf(docx_path, pdf_path)
        except LastResortError as e:
            return jsonify({
                "error": "no_converter",
                "detail": str(e),
                "hint": (
                    "Install Microsoft Word (macOS/Windows) or LibreOffice "
                    "(cross-platform), or open the .docx and File → "
                    "Save As → PDF manually."
                ),
            }), 503
        except ConversionError as e:
            return jsonify({
                "error": "conversion_failed",
                "detail": str(e),
            }), 502

        pdf_bytes = Path(result.pdf_path).read_bytes()
        resp = send_file(
            io.BytesIO(pdf_bytes),
            as_attachment=True,
            download_name=f"{base}.pdf",
            mimetype=PDF_MIME,
        )
        resp.headers["X-PDF-Converter"] = result.converter
        resp.headers["Access-Control-Expose-Headers"] = "X-PDF-Converter"
        return resp
    finally:
        for p in (docx_path, pdf_path):
            try:
                os.unlink(p)
            except OSError:
                pass


@app.route("/api/analyze-jd", methods=["POST"])
def api_analyze_jd():
    """Run the heuristic JD extractor and return signals as JSON. No LLM call."""
    jd_text = request.form.get("jd_text", "").strip()
    if not jd_text:
        return jsonify({"error": "jd_text is required"}), 400
    signals = extract_signals(jd_text)
    return jsonify(signals.for_prompt())


@app.route("/api/analyze-target-role", methods=["POST"])
def api_analyze_target_role():
    """Synthesize JDSignals from a TargetRole payload. No LLM call.

    Used by the index page's "no JD" mode to render the same JD-signals panel
    the paste path renders — so the user sees what the tailor will use.
    """
    try:
        target = _parse_target_role(request.form)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if target is None:
        return jsonify({"error": "target_role_json is required"}), 400
    signals = from_target_role(target)
    return jsonify(signals.for_prompt())


@app.route("/api/extract-text", methods=["POST"])
def api_extract_text():
    """Accept an uploaded JD file (PDF, DOCX, MD, TXT) and return its plain text.

    Master uploads stay YAML-only for now — the structured schema (bullet IDs,
    tags, impact scores) needs a separate LLM bootstrap pass (roadmap).
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "file is required"}), 400
    data = f.read()
    if not data:
        return jsonify({"error": "file is empty"}), 400
    try:
        text, kind = extract_text(f.filename, data)
    except ExtractError as e:
        return jsonify({"error": str(e)}), 415
    return jsonify({
        "filename": f.filename,
        "kind": kind,
        "text": text,
        "chars": len(text),
    })


@app.route("/api/delete-my-data", methods=["POST"])
def api_delete_my_data():
    """Wipe local artifacts the tool created (Phase 9 privacy).

    Removes:
      - ``sessions/`` (every wizard session state.yaml + chunks/)
      - ``master.yaml.bak.*`` (every backup of master.yaml)
      - ``drafts/`` (legacy directory if present from an older release)

    Explicitly does NOT touch the current ``master.yaml`` — that's the
    user's actual data; they'd delete it themselves if they wanted.

    The wipe roots can be overridden via Flask config for testing:
      ``app.config["DELETE_MY_DATA_ROOT"] = "<path>"`` overrides cwd.
      ``app.config["WIZARD_SESSIONS_ROOT"]`` is honored as well.

    Returns the list of paths removed so the caller can show a tally.
    """
    import shutil as _shutil

    root = Path(app.config.get("DELETE_MY_DATA_ROOT", Path.cwd()))
    sessions_root = Path(
        app.config.get("WIZARD_SESSIONS_ROOT", root / "sessions")
    )

    removed: list[str] = []
    errors: list[str] = []

    # 1. sessions/ — every wizard session.
    if sessions_root.exists() and sessions_root.is_dir():
        try:
            _shutil.rmtree(sessions_root)
            removed.append(str(sessions_root))
        except OSError as e:
            errors.append(f"could not remove {sessions_root}: {e}")

    # 2. master.yaml.bak.* — every timestamped backup.
    try:
        for backup in sorted(root.glob("master.yaml.bak.*")):
            if backup.is_file():
                try:
                    backup.unlink()
                    removed.append(str(backup))
                except OSError as e:
                    errors.append(f"could not remove {backup}: {e}")
    except OSError as e:
        errors.append(f"could not scan for backups in {root}: {e}")

    # 3. drafts/ — legacy directory if present.
    drafts_dir = root / "drafts"
    if drafts_dir.exists() and drafts_dir.is_dir():
        try:
            _shutil.rmtree(drafts_dir)
            removed.append(str(drafts_dir))
        except OSError as e:
            errors.append(f"could not remove {drafts_dir}: {e}")

    # 4. applications.json — the generation log (JD snippets + ATS history).
    apps_file = Path(app.config.get("APPLICATIONS_PATH", root / "applications.json"))
    if apps_file.is_file():
        try:
            apps_file.unlink()
            removed.append(str(apps_file))
        except OSError as e:
            errors.append(f"could not remove {apps_file}: {e}")

    payload = {
        "removed_paths": removed,
        "removed_count": len(removed),
        "errors": errors,
    }
    return jsonify(payload), (200 if not errors else 207)


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True})


def main() -> int:
    host = os.environ.get("RESUME_BUILDER_HOST", "127.0.0.1")
    port = int(os.environ.get("RESUME_BUILDER_PORT", "5005"))
    url = f"http://{host}:{port}"
    print(f"Resume Builder UI -> {url}", flush=True)

    # Pop the browser open so non-developers don't have to copy a URL. Fires
    # from a background timer just after the server starts accepting
    # connections. Opt out with RESUME_BUILDER_NO_BROWSER=1 (CI / headless).
    if os.environ.get("RESUME_BUILDER_NO_BROWSER", "") not in ("1", "true", "yes"):
        import threading
        import webbrowser

        def _open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                pass  # a missing/unavailable browser must never crash the server

        threading.Timer(1.0, _open_browser).start()

    app.run(host=host, port=port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
