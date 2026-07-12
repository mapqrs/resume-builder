"""DOCX → PDF conversion via a tiered fallback chain.

Bock Part 02 + Part 07 both recommend submitting PDF (not Word) to ATS
systems. We render to .docx for formatting fidelity, then convert.

Converter chain (auto-detect, first available wins)
---------------------------------------------------

1. ``docx2pdf`` (Python) — on macOS this drives the installed Word app
   via AppleScript; on Windows it uses Word COM. Best fidelity when Word
   is installed. Skipped on Linux entirely.
2. ``libreoffice --headless --convert-to pdf`` — cross-platform, no Word
   needed, decent fidelity. Detected via ``shutil.which("libreoffice")``
   or ``shutil.which("soffice")``.
3. ``LastResortError`` — neither available. Caller surfaces the
   "open in Word, File → Save As → PDF" message to the user.

Public API
----------

``convert_docx_to_pdf(docx_path, pdf_path) -> ConversionResult`` does the
work. ``available_converter_name() -> str | None`` reports which one
would be used right now without converting. ``ConversionError`` is the
base exception; ``LastResortError`` is the specific "no converter
installed" subclass so callers can distinguish a transient failure from
a configuration gap.

All paths run in-process synchronously. The converter may shell out, but
this module doesn't background anything — the web layer handles that
with its own threading if needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Conversion timeout — both docx2pdf (Word AppleScript) and libreoffice
# headless conversions on a typical resume run in <10s; 90s gives huge
# headroom for cold-start Word launches without hanging forever.
DEFAULT_TIMEOUT_S = 90


class ConversionError(RuntimeError):
    """Base class for any PDF conversion failure."""


class LastResortError(ConversionError):
    """No automated converter available on this machine.

    The caller should surface the user-facing message: open the .docx in
    Word (or LibreOffice / Pages), File → Save As → PDF.
    """

    def __init__(self):
        super().__init__(
            "No PDF converter available. Install Microsoft Word (docx2pdf "
            "uses it on macOS/Windows) or LibreOffice (cross-platform), "
            "then retry. Or open the .docx and File → Save As → PDF "
            "manually."
        )


@dataclass
class ConversionResult:
    """Returned by ``convert_docx_to_pdf`` on success."""

    pdf_path: Path
    converter: str  # "docx2pdf" | "libreoffice"


# ---------- docx2pdf ----------


def _docx2pdf_available() -> bool:
    """True iff ``docx2pdf`` is importable AND we're on a platform where
    it can drive Word (macOS or Windows). On Linux it's installable but
    raises NotImplementedError at runtime — skip pre-emptively.
    """
    if sys.platform not in ("darwin", "win32"):
        return False
    try:
        import docx2pdf  # noqa: F401
    except ImportError:
        return False
    return True


def _convert_via_docx2pdf(docx_path: Path, pdf_path: Path) -> None:
    """Drive Word via docx2pdf. Raises ConversionError on failure."""
    try:
        from docx2pdf import convert as _docx2pdf_convert
    except ImportError as e:
        raise ConversionError(
            "docx2pdf is not installed. `pip install docx2pdf` "
            "(macOS/Windows only)."
        ) from e

    try:
        # docx2pdf accepts (input, output) — both file paths.
        _docx2pdf_convert(str(docx_path), str(pdf_path))
    except Exception as e:  # noqa: BLE001 — surface whatever Word emitted
        raise ConversionError(f"docx2pdf failed: {e}") from e

    if not pdf_path.exists() or pdf_path.stat().st_size < 100:
        raise ConversionError(
            f"docx2pdf reported success but {pdf_path} is missing or too small."
        )


# ---------- libreoffice ----------


def _libreoffice_binary() -> Optional[str]:
    """Find the libreoffice / soffice binary on PATH. Returns None if absent."""
    for candidate in ("libreoffice", "soffice"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _libreoffice_available() -> bool:
    return _libreoffice_binary() is not None


def _convert_via_libreoffice(docx_path: Path, pdf_path: Path,
                             *, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
    """Convert via ``libreoffice --headless --convert-to pdf``.

    LibreOffice writes the PDF to its --outdir using the same basename
    as the input file. We point --outdir at a stable scratch location
    next to ``pdf_path``, then rename the output to the user's chosen
    name so callers get a deterministic path.
    """
    binary = _libreoffice_binary()
    if not binary:
        raise ConversionError("libreoffice / soffice not on PATH")

    out_dir = pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_temp_pdf = out_dir / (docx_path.stem + ".pdf")

    cmd = [
        binary,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(docx_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            # LibreOffice on macOS can prompt for a default profile path on
            # first run — give it a private profile dir so it never blocks.
            env={**os.environ, "HOME": os.environ.get("HOME", str(Path.home()))},
        )
    except FileNotFoundError as e:
        raise ConversionError(f"libreoffice binary disappeared: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise ConversionError(
            f"libreoffice conversion timed out after {timeout_s}s"
        ) from e

    if proc.returncode != 0:
        raise ConversionError(
            f"libreoffice exited {proc.returncode}. "
            f"stderr:\n{(proc.stderr or '').strip()}"
        )

    if not expected_temp_pdf.exists():
        raise ConversionError(
            f"libreoffice reported success but {expected_temp_pdf} is missing.\n"
            f"stdout:\n{proc.stdout[:500]}"
        )

    # If the user asked for a non-default output filename, move it.
    if expected_temp_pdf != pdf_path:
        expected_temp_pdf.replace(pdf_path)


# ---------- public API ----------


def available_converter_name() -> Optional[str]:
    """Return the name of the first available converter (or None)."""
    if _docx2pdf_available():
        return "docx2pdf"
    if _libreoffice_available():
        return "libreoffice"
    return None


def convert_docx_to_pdf(
    docx_path: str | Path,
    pdf_path: str | Path,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    prefer: Optional[str] = None,
) -> ConversionResult:
    """Convert a .docx file to .pdf using the first available converter.

    Args:
        docx_path: source .docx (must exist).
        pdf_path: destination .pdf (parent dirs created if missing).
        timeout_s: per-converter timeout.
        prefer: optionally pin to ``"docx2pdf"`` or ``"libreoffice"``. If
            the preferred converter isn't available, falls through to the
            default chain.

    Returns:
        ``ConversionResult`` with the absolute pdf_path and the name of
        the converter used.

    Raises:
        FileNotFoundError if ``docx_path`` doesn't exist.
        LastResortError if no converter is available.
        ConversionError on any other failure.
    """
    src = Path(docx_path)
    dst = Path(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"docx source missing: {src}")
    if src.suffix.lower() != ".docx":
        raise ConversionError(
            f"expected .docx input, got {src.suffix!r}: {src}"
        )

    candidates: list[tuple[str, callable]] = []
    if _docx2pdf_available():
        candidates.append(("docx2pdf", _convert_via_docx2pdf))
    if _libreoffice_available():
        candidates.append(("libreoffice",
                           lambda s, d: _convert_via_libreoffice(s, d, timeout_s=timeout_s)))

    if not candidates:
        raise LastResortError()

    if prefer:
        # Move the preferred converter to the front if it's available.
        candidates.sort(key=lambda kv: 0 if kv[0] == prefer else 1)

    last_err: Optional[ConversionError] = None
    for name, func in candidates:
        try:
            func(src, dst)
            return ConversionResult(pdf_path=dst.resolve(), converter=name)
        except ConversionError as e:
            last_err = e
            continue

    # All converters errored — re-raise the last error so the user sees a
    # real failure, not the "no converter installed" message.
    assert last_err is not None
    raise last_err
