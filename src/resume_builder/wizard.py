"""Flask blueprint for the master-resume bootstrap wizard.

Mounted at ``/wizard`` (UI) and ``/api/wizard/*`` (JSON). Holds the entire
Phase 1 brain-dump surface: role family + career start + editable chunks +
per-chunk free-text dump with auto-save persistence.

Sessions are persisted via :mod:`session_store` to ``sessions/<id>/state.yaml``.
Each request that touches a session calls ``session_store.save`` so a tab
crash or accidental refresh never costs the user their work.

All endpoints accept JSON; the UI auto-saves via PATCH on every edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
)

from datetime import datetime, timezone

import yaml as _yaml

from . import (
    bootstrap,
    linkedin_builder,
    promote as promote_module,
    role_families,
    session_store,
)
from . import resume_import
from .file_extract import ExtractError, extract_text
from .llm import CopyPasteRequired, LLMError, pick_provider, provider_status
from .schema import Basics, Education
from .session_store import (
    CADENCE_GUIDANCE,
    CADENCE_MONTHS,
    BootstrapSession,
    ChunkEmployment,
    DraftAccomplishment,
    TimeChunk,
    default_chunks_for,
    delete_session,
    list_sessions,
    load,
    new_session,
    save,
    suggest_cadence,
)
from .wizard_prompts import reflection_prompts


wizard_bp = Blueprint("wizard", __name__)


# ---------- helpers ----------


def _sessions_root() -> Path:
    """Where wizard sessions live. Configurable via Flask config for tests."""
    return Path(current_app.config.get(
        "WIZARD_SESSIONS_ROOT", session_store.DEFAULT_SESSIONS_DIR
    ))


def _load(session_id: str) -> BootstrapSession:
    try:
        return load(session_id, sessions_root=_sessions_root())
    except FileNotFoundError:
        abort(404, description=f"session {session_id} not found")


def _save(session: BootstrapSession) -> Path:
    return save(session, sessions_root=_sessions_root())


def _session_payload(s: BootstrapSession) -> dict[str, Any]:
    """Serialise a session for the API. Adds the per-chunk prompts inline
    so the UI doesn't need a second round-trip per chunk.

    Also bundles the cadence guidance + a suggestion based on the user's
    career_start (Phase 10 item 2) so the wizard's cadence picker can
    render copy + pre-select sensibly without a second round-trip.
    """
    data = s.model_dump(mode="json")
    data["prompts"] = reflection_prompts(s.role_family)
    data["role_families"] = [
        {"id": rf.id, "label": rf.label, "blurb": rf.blurb}
        for rf in role_families.all_families()
    ]
    # Cadence picker meta — same shape every request so the UI can re-render.
    data["cadence_options"] = [
        {
            "id": cad_id,
            "label": CADENCE_GUIDANCE[cad_id]["label"],
            "best_for": CADENCE_GUIDANCE[cad_id]["best_for"],
            "notes": CADENCE_GUIDANCE[cad_id]["notes"],
            "months": CADENCE_MONTHS[cad_id],
        }
        for cad_id in ("monthly", "quarterly", "six-monthly", "annual")
    ]
    data["suggested_cadence"] = (
        suggest_cadence(s.career_start) if s.career_start else None
    )
    return data


# ---------- UI ----------


@wizard_bp.route("/wizard", methods=["GET"])
def wizard_page():
    """Render the wizard SPA shell. Session id passed as ``?session=<id>``;
    if absent or unknown, the client calls ``POST /api/wizard`` to mint one.
    """
    return render_template(
        "wizard.html",
        role_families=role_families.all_families(),
        provider_status=provider_status(),
    )


# ---------- API ----------


@wizard_bp.route("/api/wizard", methods=["POST"])
def create_session():
    """Create a fresh wizard session. Returns the new session payload."""
    s = new_session()
    _save(s)
    return jsonify(_session_payload(s)), 201


@wizard_bp.route("/api/wizard/sessions", methods=["GET"])
def sessions_index():
    """List all wizard sessions, newest first, for the "your sessions" gallery.

    Each entry carries just enough to render a card: a human label (basics
    name → newest employment → role family → id), timestamps, and progress
    counts. Static path segment wins over the ``<session_id>`` converter,
    so this never shadows a real session id.
    """
    root = _sessions_root()
    entries: list[dict[str, Any]] = []
    for sid in list_sessions(sessions_root=root):
        try:
            s = load(sid, sessions_root=root)
        except Exception:
            continue  # unreadable state file — skip, never break the gallery
        if s.basics and s.basics.name.strip():
            label = s.basics.name.strip()
        elif s.employment:
            newest = s.employment[-1]
            label = " — ".join(x for x in (newest.company, newest.role) if x) or sid
        elif s.role_family:
            label = (s.role_family_other or s.role_family).replace("-", " ").title()
        else:
            label = f"Session {sid[:6]}"
        entries.append({
            "id": s.id,
            "label": label,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "chunks": len(s.chunks),
            "dumped_chunks": sum(1 for c in s.chunks if (c.raw_notes or "").strip()),
            "drafts": len(s.drafts),
            "promoted": bool(s.promoted_master_path),
        })
    return jsonify({"sessions": entries})


@wizard_bp.route("/api/wizard/<session_id>", methods=["DELETE"])
def delete_session_route(session_id: str):
    """Delete one wizard session directory. 404 when it doesn't exist."""
    if not delete_session(session_id, sessions_root=_sessions_root()):
        abort(404, description=f"session {session_id} not found")
    return jsonify({"ok": True, "deleted": session_id})


@wizard_bp.route("/api/wizard/<session_id>", methods=["GET"])
def get_session(session_id: str):
    return jsonify(_session_payload(_load(session_id)))


@wizard_bp.route("/api/wizard/<session_id>", methods=["PATCH"])
def patch_session(session_id: str):
    """Merge a partial update into the session.

    Accepts any subset of ``role_family``, ``role_family_other``,
    ``career_start``, ``notes``, plus a ``chunks`` array which replaces the
    full chunk list (the UI is the source of truth for chunk ordering /
    editing). ``raw_notes`` per chunk are accepted as part of that array.
    """
    s = _load(session_id)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        abort(400, description="patch payload must be a JSON object")

    _apply_patch(s, payload)
    _save(s)
    return jsonify(_session_payload(s))


@wizard_bp.route(
    "/api/wizard/<session_id>/regenerate-chunks", methods=["POST"],
)
def regenerate_chunks(session_id: str):
    """Recompute the chunk list from ``career_start`` and optional cadence.

    Preserves ``raw_notes`` keyed by chunk id so user-typed content survives
    a regeneration if the chunk boundaries don't shift.

    Body (optional):
      {"cadence": "monthly"|"quarterly"|"six-monthly"|"annual"}
      If absent and the session has no saved cadence, auto-picks via
      ``chunk_size_months``. A supplied cadence is persisted on the session
      so subsequent regenerations keep it.
    """
    s = _load(session_id)
    if not s.career_start:
        abort(400, description="career_start must be set before regenerating chunks")

    payload = request.get_json(silent=True) or {}
    cadence = payload.get("cadence")
    if cadence is not None:
        if cadence not in CADENCE_MONTHS:
            abort(400, description=(
                f"unknown cadence {cadence!r}; "
                f"valid: {sorted(CADENCE_MONTHS)}"
            ))
        s.cadence = cadence

    preserved: dict[str, str] = {c.id: c.raw_notes for c in s.chunks if c.raw_notes}
    s.chunks = default_chunks_for(s.career_start, cadence=s.cadence)
    for c in s.chunks:
        if c.id in preserved:
            c.raw_notes = preserved[c.id]

    _save(s)
    return jsonify(_session_payload(s))


@wizard_bp.route(
    "/api/wizard/<session_id>/chunks/<chunk_id>/extract", methods=["POST"],
)
def extract_chunk(session_id: str, chunk_id: str):
    """Run the LLM extract for one chunk's raw notes.

    Skips chunks below the ``MIN_CHUNK_CHARS`` threshold with a 422 (so the
    UI can surface the "type more first" hint without treating it as a
    server error). Existing drafts for the chunk are merged via
    ``merge_drafts_preserving_confirmed`` so any draft the user has
    already approved (``user_confirmed=true``) survives a re-run.

    Request body (optional):
      {"replace": true}    — confirm replacement when existing drafts exist
                            (matches the warn-then-replace UI flow).
    """
    s = _load(session_id)
    chunk = next((c for c in s.chunks if c.id == chunk_id), None)
    if chunk is None:
        abort(404, description=f"chunk {chunk_id} not found in session {session_id}")

    if bootstrap.too_short(chunk):
        return (
            jsonify({
                "error": "chunk_too_short",
                "min_chars": bootstrap.MIN_CHUNK_CHARS,
                "current_chars": len((chunk.raw_notes or "").strip()),
                "hint": (
                    f"Type at least {bootstrap.MIN_CHUNK_CHARS} characters of "
                    "raw notes before extracting accomplishments."
                ),
            }),
            422,
        )

    payload = request.get_json(silent=True) or {}
    replace_confirmed = bool(payload.get("replace"))
    existing_for_chunk = [d for d in s.drafts if d.chunk_id == chunk_id]
    if existing_for_chunk and not replace_confirmed:
        return (
            jsonify({
                "error": "drafts_exist",
                "existing_count": len(existing_for_chunk),
                "confirmed_count": sum(1 for d in existing_for_chunk if d.user_confirmed),
                "hint": (
                    "Drafts already exist for this chunk. Re-send with "
                    "{'replace': true} to refresh; confirmed drafts will be kept."
                ),
            }),
            409,
        )

    try:
        choice = pick_provider()
    except LLMError as e:  # pragma: no cover — pick_provider always returns
        abort(500, description=str(e))

    try:
        fresh, user_prompt, raw_response = bootstrap.extract_drafts(
            chunk, choice.provider,
            role_family=s.role_family,
            role_family_other=s.role_family_other,
        )
    except CopyPasteRequired:
        return (
            jsonify({
                "error": "copy_paste_required",
                "hint": (
                    "No automated AI access available. Log in to Claude Code "
                    "or set ANTHROPIC_API_KEY. Copy-paste flow for the wizard "
                    "extract is on the roadmap."
                ),
            }),
            502,
        )
    except bootstrap.ExtractError as e:
        return jsonify({"error": "extract_failed", "detail": str(e)}), 502
    except LLMError as e:
        return jsonify({"error": "llm_failed", "detail": str(e)}), 502

    # Replace this chunk's drafts; preserve user_confirmed ones from the
    # previous run; leave other chunks' drafts untouched.
    other_chunks = [d for d in s.drafts if d.chunk_id != chunk_id]
    merged = bootstrap.merge_drafts_preserving_confirmed(existing_for_chunk, fresh)
    s.drafts = other_chunks + merged
    _save(s)

    fresh_ids = {d.id for d in fresh}
    return jsonify({
        "session": _session_payload(s),
        "extracted_count": len(fresh),
        "preserved_count": len(merged) - len(fresh),
        "drafts": [d.model_dump(mode="json") for d in merged],
        "fresh_draft_ids": sorted(fresh_ids),
        "provider": {"name": choice.provider.name, "reason": choice.reason},
        "llm_call": {
            "system_prompt": bootstrap.SYSTEM_PROMPT,
            "user_message": user_prompt,
            "raw_response": raw_response,
        },
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/categorize", methods=["POST"],
)
def categorize(session_id: str):
    """Slot each unbucketed draft into one of the 7 canonical buckets.

    Idempotent: drafts that already have a ``bucket`` are left untouched —
    so re-running this never overwrites a user's manual reassignment.
    Returns the full session payload plus per-draft rationales and the
    raw LLM call so the transparency toggle can render it.
    """
    s = _load(session_id)
    if not s.drafts:
        return jsonify({
            "session": _session_payload(s),
            "assigned_count": 0,
            "assignments": {},
            "rationales": {},
            "llm_call": None,
            "hint": "No drafts to categorize yet — run Extract first.",
        })

    needs_assignment = [d for d in s.drafts if not d.bucket]
    if not needs_assignment:
        return jsonify({
            "session": _session_payload(s),
            "assigned_count": 0,
            "assignments": {},
            "rationales": {},
            "llm_call": None,
            "hint": (
                "All drafts already have a bucket. Reassign via the dropdown "
                "on a card or delete a bucket to re-categorize."
            ),
        })

    try:
        choice = pick_provider()
    except LLMError as e:  # pragma: no cover — pick_provider always returns
        abort(500, description=str(e))

    try:
        assignments, user_prompt, raw_response = bootstrap.categorize_drafts(
            s.drafts, choice.provider,
            role_family=s.role_family,
            role_family_other=s.role_family_other,
        )
    except CopyPasteRequired:
        return (
            jsonify({
                "error": "copy_paste_required",
                "hint": (
                    "No automated AI access available. Log in to Claude Code "
                    "or set ANTHROPIC_API_KEY. Copy-paste flow for the wizard "
                    "categorize step is on the roadmap."
                ),
            }),
            502,
        )
    except bootstrap.ExtractError as e:
        return jsonify({"error": "categorize_failed", "detail": str(e)}), 502
    except LLMError as e:
        return jsonify({"error": "llm_failed", "detail": str(e)}), 502

    rationales: dict[str, str] = {}
    assigned_count = 0
    for d in s.drafts:
        info = assignments.get(d.id)
        if info is None:
            continue
        d.bucket = info["bucket"]
        rationales[d.id] = info["rationale"]
        assigned_count += 1
    _save(s)

    return jsonify({
        "session": _session_payload(s),
        "assigned_count": assigned_count,
        "assignments": {did: info["bucket"] for did, info in assignments.items()},
        "rationales": rationales,
        "provider": {"name": choice.provider.name, "reason": choice.reason},
        "llm_call": {
            "system_prompt": bootstrap.CATEGORIZE_SYSTEM_PROMPT,
            "user_message": user_prompt,
            "raw_response": raw_response,
        },
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/drafts/<draft_id>/merge", methods=["POST"],
)
def merge_drafts(session_id: str, draft_id: str):
    """Structurally fuse two drafts into one new draft.

    Body: ``{"with": "<other-draft-id>"}``. Both originals are removed
    from the session; the merged draft inherits provenance from both
    (raw_quote + user_followups + tags + bucket + tier).
    """
    s = _load(session_id)
    payload = request.get_json(silent=True) or {}
    other_id = payload.get("with")
    if not other_id or not isinstance(other_id, str):
        abort(400, description="`with` (other draft id) is required")
    if other_id == draft_id:
        abort(400, description="cannot merge a draft with itself")

    by_id = {d.id: d for d in s.drafts}
    a = by_id.get(draft_id)
    b = by_id.get(other_id)
    if a is None or b is None:
        abort(404, description="one or both drafts not found in session")

    merged = bootstrap.merge_two_drafts(a, b)
    keep = [d for d in s.drafts if d.id not in (draft_id, other_id)]
    s.drafts = keep + [merged]
    _save(s)

    return jsonify({
        "session": _session_payload(s),
        "merged_draft": merged.model_dump(mode="json"),
        "removed_ids": [draft_id, other_id],
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/drafts/<draft_id>/polish", methods=["POST"],
)
def polish_draft(session_id: str, draft_id: str):
    """Run the LLM polish for one draft using user-supplied follow-up answers.

    Request body:
      {"followups": {"y_metric": "...", "z_method": "...", "x_strong_verb": "..."}}

    Each key is optional. Empty / missing keys instruct the LLM to keep
    the matching placeholder in the polished output. ``user_followups`` on
    the draft accumulate across runs so the fabrication guard always treats
    everything the user has said as legal source vocabulary.
    """
    s = _load(session_id)
    idx = next((i for i, d in enumerate(s.drafts) if d.id == draft_id), -1)
    if idx < 0:
        abort(404, description=f"draft {draft_id} not found in session {session_id}")

    payload = request.get_json(silent=True) or {}
    followups = payload.get("followups") or {}
    if not isinstance(followups, dict):
        abort(400, description="`followups` must be an object")

    try:
        choice = pick_provider()
    except LLMError as e:  # pragma: no cover — pick_provider always returns
        abort(500, description=str(e))

    try:
        polished, user_prompt, raw_response, fabrication_warnings = \
            bootstrap.polish_draft(s.drafts[idx], followups, choice.provider)
    except CopyPasteRequired:
        return (
            jsonify({
                "error": "copy_paste_required",
                "hint": (
                    "No automated AI access available. Log in to Claude Code "
                    "or set ANTHROPIC_API_KEY. Copy-paste flow for polish "
                    "is on the roadmap."
                ),
            }),
            502,
        )
    except bootstrap.PolishError as e:
        return jsonify({"error": "polish_failed", "detail": str(e)}), 502
    except LLMError as e:
        return jsonify({"error": "llm_failed", "detail": str(e)}), 502

    s.drafts[idx] = polished
    _save(s)

    return jsonify({
        "session": _session_payload(s),
        "draft": polished.model_dump(mode="json"),
        "fabrication_warnings": fabrication_warnings,
        "provider": {"name": choice.provider.name, "reason": choice.reason},
        "llm_call": {
            "system_prompt": bootstrap.POLISH_SYSTEM_PROMPT,
            "user_message": user_prompt,
            "raw_response": raw_response,
        },
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/promote-preview", methods=["POST"],
)
def promote_preview(session_id: str):
    """Assemble the wizard session into a ``Master`` and return it as YAML.

    The body of the response carries the YAML text + the list of warnings
    so the UI can show both side-by-side. No disk write happens here —
    use ``/promote-save`` for that. The YAML is editable in the UI; the
    user can submit the edited form to ``/promote-save``.
    """
    s = _load(session_id)
    result = promote_module.promote_to_master(s)
    yaml_text = _yaml.safe_dump(
        result.master.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    return jsonify({
        "yaml": yaml_text,
        "warnings": [
            {"kind": w.kind, "message": w.message,
             "draft_id": w.draft_id, "chunk_id": w.chunk_id}
            for w in result.warnings
        ],
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/promote-save", methods=["POST"],
)
def promote_save(session_id: str):
    """Write the master YAML to disk.

    Request body (optional):
      {"yaml": "..."}  — submit user-edited YAML instead of the auto-assembly.

    Existing master.yaml is backed up to master.yaml.bak.<ISO8601>
    before being overwritten so the user never loses a previous version.
    The save path is recorded on ``session.promoted_master_path`` and the
    backup path (if any) is returned in the response.
    """
    s = _load(session_id)

    payload = request.get_json(silent=True) or {}
    user_yaml: Optional[str] = payload.get("yaml") if isinstance(payload, dict) else None

    if user_yaml is not None and not isinstance(user_yaml, str):
        abort(400, description="`yaml` body field must be a string")

    if user_yaml is None or not user_yaml.strip():
        result = promote_module.promote_to_master(s)
        yaml_text = _yaml.safe_dump(
            result.master.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        )
        warnings_payload = [
            {"kind": w.kind, "message": w.message,
             "draft_id": w.draft_id, "chunk_id": w.chunk_id}
            for w in result.warnings
        ]
    else:
        # Validate the user's edited YAML round-trips through pydantic
        # before we ever touch disk.
        try:
            data = _yaml.safe_load(user_yaml) or {}
            from .schema import Master
            Master.model_validate(data)
        except Exception as e:
            return jsonify({"error": "invalid_yaml", "detail": str(e)}), 400
        yaml_text = user_yaml
        warnings_payload = []

    master_path = Path(
        current_app.config.get(
            "WIZARD_MASTER_OUTPUT_PATH",
            Path.cwd() / "master.yaml",
        )
    )
    backup_path: Optional[Path] = None
    if master_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = master_path.with_name(f"master.yaml.bak.{stamp}")
        master_path.replace(backup_path)

    master_path.parent.mkdir(parents=True, exist_ok=True)
    master_path.write_text(yaml_text, encoding="utf-8")

    s.promoted_master_path = str(master_path)
    _save(s)

    return jsonify({
        "saved_path": str(master_path),
        "backup_path": str(backup_path) if backup_path else None,
        "warnings": warnings_payload,
        "session": _session_payload(s),
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/linkedin", methods=["POST"],
)
def build_linkedin_profile(session_id: str):
    """Generate a complete LinkedIn profile from the wizard's promoted master.

    Source of truth: prefer the master.yaml on disk if the session has been
    promoted (``session.promoted_master_path`` is set). Otherwise assemble
    a fresh master from the session in-memory via ``promote_to_master`` —
    that way the user can preview LinkedIn output before committing to a
    Phase-6 save.
    """
    s = _load(session_id)

    master = None
    saved_path = s.promoted_master_path
    if saved_path:
        try:
            from .loaders import load_master
            master = load_master(saved_path)
        except FileNotFoundError:
            saved_path = None  # fall through to in-memory promote

    if master is None:
        promote_result = promote_module.promote_to_master(s)
        master = promote_result.master

    try:
        choice = pick_provider()
    except LLMError as e:  # pragma: no cover — pick_provider always returns
        abort(500, description=str(e))

    try:
        profile = linkedin_builder.build_linkedin(master, choice.provider)
    except CopyPasteRequired:
        return (
            jsonify({
                "error": "copy_paste_required",
                "hint": (
                    "No automated AI access available. Log in to Claude Code "
                    "or set ANTHROPIC_API_KEY. Copy-paste flow for the LinkedIn "
                    "builder is on the roadmap."
                ),
            }),
            502,
        )
    except linkedin_builder.LinkedInBuildError as e:
        return jsonify({"error": "linkedin_failed", "detail": str(e)}), 502
    except LLMError as e:
        return jsonify({"error": "llm_failed", "detail": str(e)}), 502

    guard = linkedin_builder.validate_linkedin(master, profile)
    plain_text = linkedin_builder.linkedin_to_plain_text(profile, master)

    return jsonify({
        "profile": profile.model_dump(mode="json"),
        "plain_text": plain_text,
        "warnings": [
            {"section": w.section, "reason": w.reason, "text": w.text}
            for w in guard.warnings
        ],
        "master_source": "saved_yaml" if saved_path else "in_memory_promote",
        "saved_master_path": saved_path,
        "provider": {"name": choice.provider.name, "reason": choice.reason},
    })


@wizard_bp.route("/api/wizard/where-to-look", methods=["GET"])
def where_to_look():
    """Static recall hints surfaced by the polish UI when a draft is missing
    a metric / method / strong verb. Kept server-side so a future tweak to
    the hint copy doesn't need a frontend redeploy.
    """
    return jsonify(bootstrap.WHERE_TO_LOOK)


@wizard_bp.route(
    "/api/wizard/<session_id>/import-resume", methods=["POST"],
)
def import_resume(session_id: str):
    """Extract text from an uploaded PDF / DOCX / MD / TXT and return it.

    The wizard UI decides which chunk (if any) to seed with the result —
    this endpoint just does the extraction. Reuses the existing
    ``file_extract.extract_text`` so the supported formats stay in sync
    with the JD upload path.
    """
    _load(session_id)  # 404 early if the session id is bogus
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, description="no file uploaded")

    data = f.read()
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


@wizard_bp.route(
    "/api/wizard/<session_id>/import-apply", methods=["POST"],
)
def import_apply(session_id: str):
    """Parse an existing resume and pre-fill the whole session from it.

    Accepts either a multipart ``file`` (PDF/DOCX/MD/TXT, extracted via
    ``file_extract``) or JSON ``{"text": "..."}``. One LLM call parses the
    text into basics / employment / education / skills; ``apply_import``
    maps that onto the session (one chunk per job, seeded with the resume's
    own bullets). The user then reviews each wizard step as usual.

    Guards:
      - 422 when the text is too short to plausibly be a resume.
      - 409 when the session already has typed content and ``force`` isn't
        set (warn-then-replace, same pattern as chunk extract).

    Copy-paste path: when no automated provider is available, returns 200
    with ``copy_paste_required`` + the exact prompts; the client then POSTs
    Claude's reply to ``import-apply-response``.
    """
    s = _load(session_id)

    f = request.files.get("file")
    if f is not None and f.filename:
        try:
            text, _kind = extract_text(f.filename, f.read())
        except ExtractError as e:
            return jsonify({"error": str(e)}), 415
        force = request.form.get("force") in ("1", "true", "yes")
    else:
        payload = request.get_json(silent=True) or {}
        text = payload.get("text") or ""
        force = bool(payload.get("force"))

    text = text.strip()
    if len(text) < resume_import.MIN_IMPORT_CHARS:
        return (
            jsonify({
                "error": "text_too_short",
                "min_chars": resume_import.MIN_IMPORT_CHARS,
                "current_chars": len(text),
                "hint": (
                    "That doesn't look like a full resume. Paste the whole "
                    "thing, or upload the PDF/DOCX."
                ),
            }),
            422,
        )

    if resume_import.session_has_content(s) and not force:
        return (
            jsonify({
                "error": "session_has_content",
                "hint": (
                    "This session already has typed notes, drafts, education, "
                    "or basics. Re-send with {'force': true} to replace them "
                    "with the imported resume."
                ),
            }),
            409,
        )

    try:
        choice = pick_provider()
    except LLMError as e:  # pragma: no cover — pick_provider always returns
        abort(500, description=str(e))

    user_msg = resume_import.build_import_user_message(text)
    try:
        raw = choice.provider.complete(
            resume_import.IMPORT_SYSTEM_PROMPT, user_msg,
        )
    except CopyPasteRequired:
        return jsonify({
            "copy_paste_required": True,
            "system_prompt": resume_import.IMPORT_SYSTEM_PROMPT,
            "user_message": user_msg,
            "hint": (
                "No automated AI access. Paste the prompt into any Claude "
                "session, then paste the JSON reply back into the wizard."
            ),
        })
    except LLMError as e:
        return jsonify({"error": "llm_failed", "detail": str(e)}), 502

    try:
        parsed = resume_import.parse_import_response(raw)
    except resume_import.ImportParseError as e:
        return jsonify({"error": "parse_failed", "detail": str(e)}), 502

    summary = resume_import.apply_import(s, parsed)
    _save(s)
    return jsonify({
        "session": _session_payload(s),
        "summary": summary.model_dump(mode="json"),
        "provider": {"name": choice.provider.name, "reason": choice.reason},
        "llm_call": {
            "system_prompt": resume_import.IMPORT_SYSTEM_PROMPT,
            "user_message": user_msg,
            "raw_response": raw,
        },
    })


@wizard_bp.route(
    "/api/wizard/<session_id>/import-apply-response", methods=["POST"],
)
def import_apply_response(session_id: str):
    """Copy-paste completion of the import flow.

    Body: ``{"response_text": "<Claude's JSON reply>", "force": bool}``.
    Parses and applies exactly like the automated path.
    """
    s = _load(session_id)
    payload = request.get_json(silent=True) or {}
    raw = (payload.get("response_text") or "").strip()
    if not raw:
        abort(400, description="response_text is required")

    if resume_import.session_has_content(s) and not bool(payload.get("force")):
        return (
            jsonify({
                "error": "session_has_content",
                "hint": "Re-send with {'force': true} to replace existing content.",
            }),
            409,
        )

    try:
        parsed = resume_import.parse_import_response(raw)
    except resume_import.ImportParseError as e:
        return jsonify({"error": "parse_failed", "detail": str(e)}), 400

    summary = resume_import.apply_import(s, parsed)
    _save(s)
    return jsonify({
        "session": _session_payload(s),
        "summary": summary.model_dump(mode="json"),
    })


# ---------- patch merge ----------


_SIMPLE_FIELDS = (
    "role_family",
    "role_family_other",
    "career_start",
    "notes",
)


def _apply_patch(s: BootstrapSession, payload: dict[str, Any]) -> None:
    """Apply a partial update to a session in place.

    Each field is permissive: unknown keys are ignored, type errors raise
    400 via ``abort``. Kept separate from the route handler so unit tests
    can exercise the merge directly.
    """
    for key in _SIMPLE_FIELDS:
        if key in payload:
            value = payload[key]
            if value is not None and not isinstance(value, str):
                abort(400, description=f"{key} must be a string or null")
            setattr(s, key, value)

    if "role_family" in payload:
        rf = payload["role_family"]
        if rf is not None and not role_families.is_known(rf):
            abort(400, description=f"unknown role_family: {rf!r}")

    if "career_start" in payload and payload["career_start"]:
        cs = payload["career_start"]
        # Cheap shape check — full validation happens in default_chunks_for.
        if not (len(cs) == 7 and cs[4] == "-" and cs[:4].isdigit() and cs[5:].isdigit()):
            abort(400, description="career_start must be 'YYYY-MM'")

    if "drafts" in payload:
        drafts_in = payload["drafts"]
        if not isinstance(drafts_in, list):
            abort(400, description="drafts must be an array")
        try:
            s.drafts = [DraftAccomplishment.model_validate(d) for d in drafts_in]
        except Exception as e:  # pydantic validation
            abort(400, description=f"invalid draft: {e}")

    if "education" in payload:
        edu_in = payload["education"]
        if not isinstance(edu_in, list):
            abort(400, description="education must be an array")
        try:
            s.education = [Education.model_validate(e) for e in edu_in]
        except Exception as e:  # pydantic validation
            abort(400, description=f"invalid education entry: {e}")

    if "basics" in payload:
        basics_in = payload["basics"]
        if basics_in is None:
            s.basics = None
        elif not isinstance(basics_in, dict):
            abort(400, description="basics must be an object or null")
        else:
            try:
                s.basics = Basics.model_validate(basics_in)
            except Exception as e:
                abort(400, description=f"invalid basics: {e}")

    if "summary" in payload:
        summary_in = payload["summary"]
        if summary_in is not None and not isinstance(summary_in, str):
            abort(400, description="summary must be a string or null")
        s.summary = summary_in

    if "employment" in payload:
        emp_in = payload["employment"]
        if not isinstance(emp_in, list):
            abort(400, description="employment must be an array")
        try:
            s.employment = [ChunkEmployment.model_validate(e) for e in emp_in]
        except Exception as e:
            abort(400, description=f"invalid employment entry: {e}")

    if "chunks" in payload:
        chunks_in = payload["chunks"]
        if not isinstance(chunks_in, list):
            abort(400, description="chunks must be an array")
        try:
            new_chunks = [TimeChunk.model_validate(c) for c in chunks_in]
        except Exception as e:  # pydantic validation
            abort(400, description=f"invalid chunk: {e}")
        for c in new_chunks:
            if not _looks_like_year_month(c.start):
                abort(400, description=f"chunk {c.id} start must be 'YYYY-MM', got {c.start!r}")
            if not _looks_like_year_month(c.end):
                abort(400, description=f"chunk {c.id} end must be 'YYYY-MM', got {c.end!r}")
            if c.start > c.end:
                abort(400, description=f"chunk {c.id}: start {c.start} is after end {c.end}")
        s.chunks = new_chunks


def _looks_like_year_month(s: str) -> bool:
    """Cheap shape check matching `YYYY-MM` with month in 01-12."""
    if not isinstance(s, str) or len(s) != 7 or s[4] != "-":
        return False
    year, month = s[:4], s[5:]
    if not (year.isdigit() and month.isdigit()):
        return False
    return 1 <= int(month) <= 12
