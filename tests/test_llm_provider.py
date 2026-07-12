"""Tests for the LLMProvider abstraction in resume_builder.llm."""

from __future__ import annotations

import json
import subprocess

import pytest

from resume_builder import llm
from resume_builder.llm import (
    AnthropicAPIProvider,
    ClaudeCodeProvider,
    CopyPasteProvider,
    CopyPasteRequired,
    LLMError,
    LLMProvider,
    ProviderChoice,
    pick_provider,
)


# ---------- CopyPasteProvider ----------


def test_copy_paste_always_available():
    assert CopyPasteProvider.is_available() is True


def test_copy_paste_raises_with_prompt():
    p = CopyPasteProvider()
    with pytest.raises(CopyPasteRequired) as exc_info:
        p.complete("system text", "user text")
    err = exc_info.value
    assert err.system_prompt == "system text"
    assert err.user_message == "user text"


def test_copy_paste_required_is_llm_error():
    # callers that catch LLMError should NOT swallow CopyPasteRequired —
    # but it should still be an LLMError so direct except chains compile.
    assert issubclass(CopyPasteRequired, LLMError)


# ---------- ClaudeCodeProvider ----------


def test_claude_code_is_available_uses_which(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert ClaudeCodeProvider.is_available() is True

    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert ClaudeCodeProvider.is_available() is False


class _FakeProc:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_claude_code_complete_happy_path(monkeypatch):
    envelope = {"result": "{\"summary\": \"ok\"}", "is_error": False}
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **kw: _FakeProc(stdout=json.dumps(envelope)),
    )
    p = ClaudeCodeProvider()
    out = p.complete("sys", "user", model="sonnet", timeout_s=10)
    assert out == "{\"summary\": \"ok\"}"


def test_claude_code_complete_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **kw: _FakeProc(stdout="", stderr="boom", returncode=2),
    )
    p = ClaudeCodeProvider()
    with pytest.raises(LLMError, match="exited 2"):
        p.complete("sys", "user")


def test_claude_code_complete_envelope_error_field(monkeypatch):
    envelope = {"result": "rate limited", "is_error": True, "api_error_status": 429}
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **kw: _FakeProc(stdout=json.dumps(envelope)),
    )
    p = ClaudeCodeProvider()
    with pytest.raises(LLMError, match="429"):
        p.complete("sys", "user")


def test_claude_code_complete_empty_result(monkeypatch):
    envelope = {"result": "", "is_error": False}
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **kw: _FakeProc(stdout=json.dumps(envelope)),
    )
    p = ClaudeCodeProvider()
    with pytest.raises(LLMError, match="empty result"):
        p.complete("sys", "user")


def test_claude_code_complete_invalid_json_stdout(monkeypatch):
    monkeypatch.setattr(
        llm.subprocess, "run",
        lambda *a, **kw: _FakeProc(stdout="not json at all"),
    )
    p = ClaudeCodeProvider()
    with pytest.raises(LLMError, match="not return valid JSON"):
        p.complete("sys", "user")


def test_claude_code_complete_cli_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("claude not found")
    monkeypatch.setattr(llm.subprocess, "run", boom)
    p = ClaudeCodeProvider()
    with pytest.raises(LLMError, match="not found on PATH"):
        p.complete("sys", "user")


def test_claude_code_complete_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10)
    monkeypatch.setattr(llm.subprocess, "run", boom)
    p = ClaudeCodeProvider()
    with pytest.raises(LLMError, match="timed out"):
        p.complete("sys", "user", timeout_s=10)


# ---------- AnthropicAPIProvider ----------


def test_anthropic_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert AnthropicAPIProvider.is_available() is False


def test_anthropic_unavailable_without_sdk(monkeypatch):
    """If the package is unavailable, is_available must return False even with the key set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Force ImportError by injecting a sentinel into sys.modules.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("simulated missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert AnthropicAPIProvider.is_available() is False


# ---------- pick_provider ----------


def test_pick_provider_falls_through_to_copy_paste(monkeypatch):
    monkeypatch.setattr(ClaudeCodeProvider, "is_available", classmethod(lambda cls: False))
    monkeypatch.setattr(AnthropicAPIProvider, "is_available", classmethod(lambda cls: False))
    choice = pick_provider()
    assert isinstance(choice.provider, CopyPasteProvider)
    assert "copy-paste" in choice.reason.lower()


def test_pick_provider_prefers_claude_code_when_available(monkeypatch):
    monkeypatch.setattr(ClaudeCodeProvider, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr(AnthropicAPIProvider, "is_available", classmethod(lambda cls: True))
    choice = pick_provider()
    assert isinstance(choice.provider, ClaudeCodeProvider)
    assert "claude code" in choice.reason.lower()


def test_pick_provider_uses_api_when_claude_unavailable(monkeypatch):
    monkeypatch.setattr(ClaudeCodeProvider, "is_available", classmethod(lambda cls: False))
    monkeypatch.setattr(AnthropicAPIProvider, "is_available", classmethod(lambda cls: True))
    choice = pick_provider()
    assert isinstance(choice.provider, AnthropicAPIProvider)
    assert "anthropic_api_key" in choice.reason.lower()


def test_pick_provider_prefer_argument_overrides(monkeypatch):
    monkeypatch.setattr(ClaudeCodeProvider, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr(AnthropicAPIProvider, "is_available", classmethod(lambda cls: True))
    choice = pick_provider(prefer="anthropic-api")
    assert isinstance(choice.provider, AnthropicAPIProvider)


def test_pick_provider_returns_choice_dataclass(monkeypatch):
    monkeypatch.setattr(ClaudeCodeProvider, "is_available", classmethod(lambda cls: False))
    monkeypatch.setattr(AnthropicAPIProvider, "is_available", classmethod(lambda cls: False))
    choice = pick_provider()
    assert isinstance(choice, ProviderChoice)
    assert isinstance(choice.provider, LLMProvider)
    assert isinstance(choice.reason, str) and choice.reason
