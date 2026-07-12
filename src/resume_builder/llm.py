"""LLM provider abstraction.

Three concrete providers, auto-selected in order:

1. ``ClaudeCodeProvider`` — shells out to ``claude -p`` (Claude Code headless).
   Uses the user's existing Claude Code login. No API key required.
2. ``AnthropicAPIProvider`` — uses the official ``anthropic`` SDK with the
   ``ANTHROPIC_API_KEY`` environment variable.
3. ``CopyPasteProvider`` — always available. ``complete()`` raises
   ``CopyPasteRequired`` carrying the rendered prompt; UI / CLI catches it
   and surfaces a paste workflow.

This module deliberately knows nothing about resumes — callers compose system
+ user prompts and parse the raw text reply themselves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


CLAUDE_P_DEFAULT_MODEL = "sonnet"
API_DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_TIMEOUT_S = 180

CLAUDE_P_DISALLOWED_TOOLS = (
    "Bash", "Edit", "Write", "Read", "Glob", "Grep",
    "WebFetch", "WebSearch", "TodoWrite", "Task",
)


class LLMError(RuntimeError):
    """Base class for any provider failure."""


class CopyPasteRequired(LLMError):
    """Raised by ``CopyPasteProvider.complete``. Carries the rendered prompt so
    the caller can show it to the user and accept the pasted reply elsewhere.
    """

    def __init__(self, system_prompt: str, user_message: str):
        self.system_prompt = system_prompt
        self.user_message = user_message
        super().__init__(
            "Copy-paste required — no automated AI access available."
        )


class LLMProvider(ABC):
    """Abstract LLM provider."""

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        ...

    @abstractmethod
    def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        model: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> str:
        """Send (system, user) and return the raw response text.

        The text may include surrounding prose or fences; callers parse it.
        """


# ---------- claude -p ----------


class ClaudeCodeProvider(LLMProvider):
    """Shell-out to ``claude -p`` (Claude Code headless mode)."""

    name = "claude-code"

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("claude") is not None

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        model: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> str:
        model = model or CLAUDE_P_DEFAULT_MODEL
        cmd = [
            "claude", "-p",
            "--model", model,
            "--output-format", "json",
            "--system-prompt", system_prompt,
            "--disallowedTools", *CLAUDE_P_DISALLOWED_TOOLS,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=user_message,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError as e:
            raise LLMError(
                "`claude` CLI not found on PATH. Install Claude Code "
                "or set ANTHROPIC_API_KEY, or use copy-paste mode."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"`claude -p` timed out after {timeout_s}s") from e

        if proc.returncode != 0:
            raise LLMError(
                f"`claude -p` exited {proc.returncode}. stderr:\n{proc.stderr.strip()}"
            )

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"`claude -p` did not return valid JSON. stdout:\n{proc.stdout[:500]}"
            ) from e

        if envelope.get("is_error"):
            raise LLMError(
                f"`claude -p` reported error "
                f"(status {envelope.get('api_error_status')}): "
                f"{envelope.get('result', '<no result>')}"
            )

        result_text = envelope.get("result", "")
        if not result_text:
            raise LLMError(
                f"`claude -p` returned empty result. "
                f"Envelope: {json.dumps(envelope)[:500]}"
            )
        return result_text


# ---------- Anthropic API ----------


class AnthropicAPIProvider(LLMProvider):
    """Uses the official ``anthropic`` SDK and ``ANTHROPIC_API_KEY``.

    The SDK is imported lazily so users without it installed still get a
    usable tool — they just won't have this provider available.
    """

    name = "anthropic-api"

    @classmethod
    def is_available(cls) -> bool:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        model: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> str:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError(
                "Anthropic SDK not installed. Run `pip install anthropic` "
                "or use a different provider."
            ) from e

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY not set. Export it or use a different provider."
            )

        client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
        try:
            response = client.messages.create(
                model=model or API_DEFAULT_MODEL,
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:  # SDK raises various subclasses; surface uniformly
            raise LLMError(f"Anthropic API call failed: {e}") from e

        chunks = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
        result = "".join(chunks).strip()
        if not result:
            raise LLMError(
                f"Anthropic API returned no text content. "
                f"Response: {response.model_dump_json()[:500]}"
            )
        return result


# ---------- copy-paste ----------


class CopyPasteProvider(LLMProvider):
    """Always available; ``complete()`` raises ``CopyPasteRequired``."""

    name = "copy-paste"

    @classmethod
    def is_available(cls) -> bool:
        return True

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        model: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> str:
        raise CopyPasteRequired(system_prompt, user_message)


# ---------- selection ----------


@dataclass
class ProviderChoice:
    provider: LLMProvider
    reason: str  # one-line message for the UI / CLI


def pick_provider(
    *,
    prefer: Optional[str] = None,
) -> ProviderChoice:
    """Auto-detect the best available provider.

    Order: claude-p → Anthropic API key → copy-paste. If ``prefer`` is set
    to one of ``"claude-code" | "anthropic-api" | "copy-paste"``, that
    provider is selected if available, otherwise auto-detection continues.
    """
    candidates: list[type[LLMProvider]] = [
        ClaudeCodeProvider,
        AnthropicAPIProvider,
        CopyPasteProvider,
    ]
    if prefer:
        candidates = sorted(
            candidates,
            key=lambda c: 0 if c.name == prefer else 1,
        )

    for cls in candidates:
        if cls.is_available():
            return ProviderChoice(cls(), _reason_for(cls))
    # CopyPasteProvider.is_available always returns True, so this is unreachable.
    raise LLMError("No LLM provider available — this should be impossible.")


def _reason_for(cls: type[LLMProvider]) -> str:
    if cls is ClaudeCodeProvider:
        return "Using your Claude Code login (no API key needed)."
    if cls is AnthropicAPIProvider:
        return "Using ANTHROPIC_API_KEY environment variable."
    if cls is CopyPasteProvider:
        return (
            "No automated AI access — copy-paste mode. "
            "Install Claude Code or set ANTHROPIC_API_KEY to skip this."
        )
    return f"Using {cls.name}."


def provider_status() -> dict:
    """UI-facing summary of which provider is live *right now*.

    Returns ``{"level", "name", "label", "detail"}`` for a status banner:
    ``level`` is ``"ok"`` when generation runs automatically, ``"warn"`` when
    the user will have to copy-paste. Never raises — copy-paste is always
    available, so there is always a truthful answer.
    """
    name = pick_provider().provider.name
    if name == ClaudeCodeProvider.name:
        return {
            "level": "ok",
            "name": name,
            "label": "Connected via Claude Code",
            "detail": "Generate runs automatically using your Claude Code login — no copy-paste needed.",
        }
    if name == AnthropicAPIProvider.name:
        return {
            "level": "ok",
            "name": name,
            "label": "Connected via API key",
            "detail": "Generate runs automatically using your ANTHROPIC_API_KEY.",
        }
    return {
        "level": "warn",
        "name": name,
        "label": "Copy-paste mode",
        "detail": "No AI connection found — you'll copy each prompt into Claude and paste the reply back. "
                  "Install Claude Code or set ANTHROPIC_API_KEY for one-click generation.",
    }
