"""Turn master+JD+pointers into a TailoredResume.

Tailoring goes through the ``LLMProvider`` abstraction in :mod:`llm`, which
auto-picks the best available backend:

- ``claude -p`` (Claude Code headless) if the CLI is logged in
- ``anthropic`` SDK with ``ANTHROPIC_API_KEY`` env var
- copy-paste fallback (always works in any environment)

``tailor_via_claude_cli`` is kept as a back-compat shim that pins the
provider to ``ClaudeCodeProvider``; ``tailor_via_provider`` is the new
generic entry point.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .jd_signals import JDSignals, extract as extract_signals
from .llm import (
    CLAUDE_P_DEFAULT_MODEL,
    DEFAULT_TIMEOUT_S,
    ClaudeCodeProvider,
    CopyPasteRequired,
    LLMError,
    LLMProvider,
    pick_provider,
)
from .prompts import SYSTEM_PROMPT, build_user_message
from .schema import Master, Pointers, TailoredResume


DEFAULT_MODEL = CLAUDE_P_DEFAULT_MODEL  # back-compat


# Back-compat: existing callers ``except ClaudeCliError as e`` should keep
# working. Alias to LLMError so callers can also catch the broader class.
class ClaudeCliError(LLMError):
    pass


def _wrap_llm_error(e: LLMError) -> ClaudeCliError:
    """Re-raise an LLMError as ClaudeCliError preserving message + cause."""
    return ClaudeCliError(str(e))


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    fence_re = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)
    m = fence_re.match(s)
    if m:
        return m.group(1).strip()
    return s


def _extract_first_json_object(s: str) -> str:
    """If the response contains prose around the JSON, pull the JSON object out.
    Naive but adequate: find the first '{' and walk balanced braces.
    """
    s = _strip_code_fences(s)
    start = s.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    raise ValueError("Unbalanced JSON braces in response")


def parse_response_text(text: str) -> TailoredResume:
    """Parse a Claude response (JSON, possibly with surrounding prose / fences)
    into a TailoredResume. Public so the web UI can avoid a temp-file dance.
    """
    payload = _extract_first_json_object(text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Tailor: response was not valid JSON ({e}). First 500 chars:\n{text[:500]}"
        ) from e
    return TailoredResume.model_validate(data)


# Back-compat alias (older internal callers).
_parse_tailored = parse_response_text


# ---------- generic provider entry point ----------


def tailor_via_provider(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    provider: LLMProvider,
    *,
    model: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    signals: Optional[JDSignals] = None,
) -> TailoredResume:
    """Tailor via any ``LLMProvider``. Wraps provider errors as ``ClaudeCliError``
    (back-compat) so existing callers don't need to update except handlers.

    ``CopyPasteRequired`` propagates unchanged so the web layer can render
    the paste UI.
    """
    if signals is None:
        signals = extract_signals(jd_text)
    user_msg = build_user_message(master, jd_text, pointers, signals=signals)
    try:
        result_text = provider.complete(
            SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
        )
    except CopyPasteRequired:
        raise
    except LLMError as e:
        raise _wrap_llm_error(e) from e
    return parse_response_text(result_text)


# ---------- back-compat: pin to ClaudeCodeProvider ----------


def tailor_via_claude_cli(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    *,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    signals: Optional[JDSignals] = None,
) -> TailoredResume:
    """Call ``claude -p`` and return a parsed TailoredResume.

    Back-compat entry point. New code should call ``tailor_via_provider``
    with a provider chosen via :func:`llm.pick_provider`.
    """
    return tailor_via_provider(
        master,
        jd_text,
        pointers,
        ClaudeCodeProvider(),
        model=model,
        timeout_s=timeout_s,
        signals=signals,
    )


def tailor_auto(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    *,
    model: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    signals: Optional[JDSignals] = None,
) -> tuple[TailoredResume, str]:
    """Tailor using ``pick_provider()`` — try claude-p, then API, then copy-paste.

    Returns ``(tailored, reason)`` where ``reason`` is a one-line message
    describing which provider was picked. Raises ``CopyPasteRequired`` if no
    automated provider is available.
    """
    choice = pick_provider()
    tailored = tailor_via_provider(
        master, jd_text, pointers, choice.provider,
        model=model, timeout_s=timeout_s, signals=signals,
    )
    return tailored, choice.reason


# ---------- Mode 2: copy-paste prompt file ----------


def write_prompt_for_paste(
    master: Master,
    jd_text: str,
    pointers: Pointers,
    out_path: str | Path,
    signals: Optional[JDSignals] = None,
) -> Path:
    """Write the full prompt (system + user) to a file so the user can paste it
    into any Claude conversation. Returns the path written.
    """
    if signals is None:
        signals = extract_signals(jd_text)
    full = (
        "# === SYSTEM (paste this as the start of your message, or paste in a system-prompt slot) ===\n"
        + SYSTEM_PROMPT
        + "\n\n"
        + "# === USER MESSAGE ===\n"
        + build_user_message(master, jd_text, pointers, signals=signals)
        + "\n\n"
        + "# === INSTRUCTIONS ===\n"
        "# Paste everything above into a Claude conversation.\n"
        "# Save Claude's JSON reply (just the JSON, no surrounding prose) to a file.\n"
        "# Then re-run: python -m resume_builder ... --from-response <that-file> --out ...\n"
    )
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full, encoding="utf-8")
    return path


def parse_response_file(path: str | Path) -> TailoredResume:
    """Read a Claude response (JSON, possibly with prose around it) and return TailoredResume."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_response_text(text)
