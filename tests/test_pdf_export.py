"""Tests for pdf_export.py — converter chain, fallback behavior, web wiring.

Most tests run without a real PDF converter installed by patching the
availability probes + conversion functions. The single end-to-end test
that actually drives docx2pdf or libreoffice is gated behind
``available_converter_name()`` and skipped when neither is on the system.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from resume_builder import pdf_export
from resume_builder.pdf_export import (
    ConversionError,
    ConversionResult,
    LastResortError,
    available_converter_name,
    convert_docx_to_pdf,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ---------- helpers ----------


def _write_fake_docx(path: Path) -> None:
    """Write a tiny stub file so 'docx exists' checks pass.

    The conversion is mocked in unit tests, so the file's contents don't
    matter — only its extension and existence.
    """
    path.write_bytes(b"PK\x03\x04 fake docx stub")


def _fake_pdf_bytes() -> bytes:
    """A valid PDF magic-byte header. PyPDF won't parse it as a full doc,
    but the magic bytes are enough for the surface tests."""
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< >>\nendobj\nxref\n0 1\n0000000000 65535 f\ntrailer\n<< /Size 1 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"


# ---------- availability detection ----------


def test_available_converter_name_returns_string_or_none():
    """Smoke test: probe returns either None or a known converter name."""
    name = available_converter_name()
    assert name in (None, "docx2pdf", "libreoffice")


def test_convert_raises_when_no_converter(tmp_path, monkeypatch):
    """If both converters report unavailable, the public function raises
    LastResortError (so callers can surface the user-facing hint)."""
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: False)
    monkeypatch.setattr(pdf_export, "_libreoffice_available", lambda: False)

    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)
    with pytest.raises(LastResortError):
        convert_docx_to_pdf(docx, tmp_path / "out.pdf")


def test_convert_rejects_missing_docx(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: True)
    monkeypatch.setattr(pdf_export, "_convert_via_docx2pdf",
                        lambda s, d: d.write_bytes(_fake_pdf_bytes()))
    with pytest.raises(FileNotFoundError):
        convert_docx_to_pdf(tmp_path / "does-not-exist.docx", tmp_path / "out.pdf")


def test_convert_rejects_non_docx_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: True)
    src = tmp_path / "in.txt"
    src.write_text("not a docx")
    with pytest.raises(ConversionError, match="expected .docx"):
        convert_docx_to_pdf(src, tmp_path / "out.pdf")


# ---------- converter chain ----------


def test_docx2pdf_preferred_over_libreoffice(tmp_path, monkeypatch):
    """When both converters are available, docx2pdf wins by default
    (Word fidelity beats LibreOffice's render)."""
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: True)
    monkeypatch.setattr(pdf_export, "_libreoffice_available", lambda: True)

    called = {"name": None}

    def fake_docx2pdf(src, dst):
        called["name"] = "docx2pdf"
        Path(dst).write_bytes(_fake_pdf_bytes())

    def fake_libreoffice(src, dst, **kwargs):
        called["name"] = "libreoffice"
        Path(dst).write_bytes(_fake_pdf_bytes())

    monkeypatch.setattr(pdf_export, "_convert_via_docx2pdf", fake_docx2pdf)
    monkeypatch.setattr(pdf_export, "_convert_via_libreoffice", fake_libreoffice)

    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)
    result = convert_docx_to_pdf(docx, tmp_path / "out.pdf")
    assert called["name"] == "docx2pdf"
    assert result.converter == "docx2pdf"
    assert result.pdf_path.exists()


def test_falls_back_to_libreoffice_when_docx2pdf_fails(tmp_path, monkeypatch):
    """If docx2pdf raises mid-conversion (Word missing, crash), the
    chain falls through to libreoffice."""
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: True)
    monkeypatch.setattr(pdf_export, "_libreoffice_available", lambda: True)

    def fail_docx2pdf(src, dst):
        raise ConversionError("Word is not running")

    def ok_libreoffice(src, dst, **kwargs):
        Path(dst).write_bytes(_fake_pdf_bytes())

    monkeypatch.setattr(pdf_export, "_convert_via_docx2pdf", fail_docx2pdf)
    monkeypatch.setattr(pdf_export, "_convert_via_libreoffice", ok_libreoffice)

    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)
    result = convert_docx_to_pdf(docx, tmp_path / "out.pdf")
    assert result.converter == "libreoffice"


def test_re_raises_last_error_when_all_converters_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: True)
    monkeypatch.setattr(pdf_export, "_libreoffice_available", lambda: True)

    monkeypatch.setattr(
        pdf_export, "_convert_via_docx2pdf",
        lambda s, d: (_ for _ in ()).throw(ConversionError("docx2pdf boom")),
    )
    monkeypatch.setattr(
        pdf_export, "_convert_via_libreoffice",
        lambda s, d, **k: (_ for _ in ()).throw(ConversionError("libreoffice boom")),
    )

    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)
    with pytest.raises(ConversionError, match="libreoffice boom"):
        convert_docx_to_pdf(docx, tmp_path / "out.pdf")


def test_prefer_libreoffice_when_requested(tmp_path, monkeypatch):
    """prefer='libreoffice' picks libreoffice even when docx2pdf is available."""
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: True)
    monkeypatch.setattr(pdf_export, "_libreoffice_available", lambda: True)

    called = {"name": None}
    monkeypatch.setattr(
        pdf_export, "_convert_via_docx2pdf",
        lambda s, d: called.__setitem__("name", "docx2pdf"),
    )

    def lo(src, dst, **kwargs):
        called["name"] = "libreoffice"
        Path(dst).write_bytes(_fake_pdf_bytes())

    monkeypatch.setattr(pdf_export, "_convert_via_libreoffice", lo)

    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)
    result = convert_docx_to_pdf(docx, tmp_path / "out.pdf", prefer="libreoffice")
    assert result.converter == "libreoffice"


# ---------- libreoffice output-path normalization ----------


def test_libreoffice_renames_default_output_to_requested_name(tmp_path, monkeypatch):
    """LibreOffice always writes <stem>.pdf to --outdir; the converter
    moves it to the user's chosen pdf_path so callers get a deterministic
    output location."""
    monkeypatch.setattr(pdf_export, "_libreoffice_binary", lambda: "/fake/libreoffice")

    docx = tmp_path / "input-doc.docx"
    _write_fake_docx(docx)
    target_pdf = tmp_path / "user-chosen.pdf"

    def fake_run(cmd, **kwargs):
        # Simulate libreoffice writing <stem>.pdf to --outdir.
        outdir_idx = cmd.index("--outdir")
        outdir = Path(cmd[outdir_idx + 1])
        default_pdf = outdir / (Path(cmd[-1]).stem + ".pdf")
        default_pdf.write_bytes(_fake_pdf_bytes())

        class _Proc:
            returncode = 0
            stderr = ""
            stdout = ""
        return _Proc()

    monkeypatch.setattr(pdf_export.subprocess, "run", fake_run)
    pdf_export._convert_via_libreoffice(docx, target_pdf, timeout_s=10)
    assert target_pdf.exists()
    assert target_pdf.read_bytes().startswith(b"%PDF")
    # The intermediate stem-named file got moved, not duplicated.
    intermediate = tmp_path / "input-doc.pdf"
    assert not intermediate.exists() or intermediate == target_pdf


def test_libreoffice_surfaces_stderr_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_export, "_libreoffice_binary", lambda: "/fake/libreoffice")
    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)

    def fake_run(cmd, **kwargs):
        class _Proc:
            returncode = 1
            stderr = "Profile already in use"
            stdout = ""
        return _Proc()

    monkeypatch.setattr(pdf_export.subprocess, "run", fake_run)
    with pytest.raises(ConversionError, match="Profile already in use"):
        pdf_export._convert_via_libreoffice(docx, tmp_path / "out.pdf", timeout_s=5)


def test_libreoffice_surfaces_timeout(tmp_path, monkeypatch):
    import subprocess as _sp
    monkeypatch.setattr(pdf_export, "_libreoffice_binary", lambda: "/fake/libreoffice")
    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)

    def fake_run(cmd, **kwargs):
        raise _sp.TimeoutExpired(cmd=cmd, timeout=1)

    monkeypatch.setattr(pdf_export.subprocess, "run", fake_run)
    with pytest.raises(ConversionError, match="timed out"):
        pdf_export._convert_via_libreoffice(docx, tmp_path / "out.pdf", timeout_s=1)


# ---------- docx2pdf wrapper edge ----------


def test_docx2pdf_wrapper_validates_output_size(tmp_path, monkeypatch):
    """If docx2pdf 'succeeds' but the resulting PDF is empty/missing, we
    raise instead of pretending it worked."""
    docx = tmp_path / "in.docx"
    _write_fake_docx(docx)
    out = tmp_path / "out.pdf"

    # Stub the docx2pdf module so import succeeds, but the convert
    # produces an empty file.
    import sys, types
    fake_mod = types.ModuleType("docx2pdf")
    def fake_convert(src, dst):
        Path(dst).write_bytes(b"")  # too small
    fake_mod.convert = fake_convert
    monkeypatch.setitem(sys.modules, "docx2pdf", fake_mod)

    with pytest.raises(ConversionError, match="missing or too small"):
        pdf_export._convert_via_docx2pdf(docx, out)


# ---------- web wiring ----------


@pytest.fixture
def web_client():
    from resume_builder.web import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_pdf_converter_endpoint_reports_availability(web_client):
    res = web_client.get("/api/pdf-converter")
    assert res.status_code == 200
    body = res.get_json()
    assert "available" in body
    assert "converter" in body
    if body["available"]:
        assert body["converter"] in ("docx2pdf", "libreoffice")
        assert body["hint"] is None
    else:
        assert body["hint"]


def test_docx_to_pdf_endpoint_returns_503_when_no_converter(
    web_client, monkeypatch,
):
    monkeypatch.setattr(pdf_export, "_docx2pdf_available", lambda: False)
    monkeypatch.setattr(pdf_export, "_libreoffice_available", lambda: False)
    # Also patch the import location web.py uses, since it imported the names.
    from resume_builder import web as _web
    monkeypatch.setattr(_web, "convert_docx_to_pdf", convert_docx_to_pdf)

    data = {"file": (io.BytesIO(b"fake docx"), "resume.docx")}
    res = web_client.post(
        "/api/docx-to-pdf",
        data=data,
        content_type="multipart/form-data",
    )
    assert res.status_code == 503
    body = res.get_json()
    assert body["error"] == "no_converter"
    assert "hint" in body


def test_docx_to_pdf_endpoint_returns_pdf_when_converter_succeeds(
    web_client, monkeypatch, tmp_path,
):
    """End-to-end web path: upload fake docx, mock the converter,
    verify the response is a PDF blob with the converter header set."""
    from resume_builder import web as _web

    def fake_convert(docx_path, pdf_path, **kwargs):
        Path(pdf_path).write_bytes(_fake_pdf_bytes())
        return ConversionResult(pdf_path=Path(pdf_path), converter="libreoffice")

    monkeypatch.setattr(_web, "convert_docx_to_pdf", fake_convert)

    data = {"file": (io.BytesIO(b"PK fake docx"), "resume.docx")}
    res = web_client.post(
        "/api/docx-to-pdf",
        data=data,
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    assert res.mimetype == "application/pdf"
    assert res.data.startswith(b"%PDF")
    assert res.headers.get("X-PDF-Converter") == "libreoffice"


def test_docx_to_pdf_rejects_empty_upload(web_client):
    res = web_client.post(
        "/api/docx-to-pdf",
        data={"file": (io.BytesIO(b""), "empty.docx")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400


def test_docx_to_pdf_rejects_missing_file(web_client):
    res = web_client.post("/api/docx-to-pdf", data={})
    assert res.status_code == 400


# ---------- CLI ----------


def test_cli_format_pdf_runs_when_converter_present(monkeypatch, tmp_path):
    """When --format pdf is set and a converter is available, the CLI
    triggers conversion. We mock the converter so the test is fast +
    platform-independent."""
    from resume_builder import cli as _cli
    from resume_builder.pdf_export import ConversionResult

    converted = {"called": False, "src": None, "dst": None}

    def fake_convert(src, dst, **kwargs):
        converted["called"] = True
        converted["src"] = Path(src)
        converted["dst"] = Path(dst)
        Path(dst).write_bytes(_fake_pdf_bytes())
        return ConversionResult(pdf_path=Path(dst), converter="libreoffice")

    monkeypatch.setattr(_cli, "available_converter_name", lambda: "libreoffice")
    monkeypatch.setattr(_cli, "convert_docx_to_pdf", fake_convert)

    master = FIXTURES / "sample-master.yaml"
    out_docx = tmp_path / "resume.docx"
    rc = _cli.main([
        "--master", str(master),
        "--out", str(out_docx),
        "--no-tailor",
        "--format", "pdf",
    ])
    assert rc == 0
    assert out_docx.exists()
    assert converted["called"]
    expected_pdf = tmp_path / "resume.pdf"
    assert converted["dst"] == expected_pdf
    assert expected_pdf.exists()


def test_cli_format_pdf_warns_when_no_converter(monkeypatch, tmp_path, capsys):
    """When --format pdf is set but no converter is installed, the CLI
    surfaces a clear warning + still writes the .docx."""
    from resume_builder import cli as _cli

    monkeypatch.setattr(_cli, "available_converter_name", lambda: None)

    master = FIXTURES / "sample-master.yaml"
    out_docx = tmp_path / "resume.docx"
    rc = _cli.main([
        "--master", str(master),
        "--out", str(out_docx),
        "--no-tailor",
        "--format", "pdf",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # Hint mentions both Word and LibreOffice so the user knows what to install.
    assert "no converter available" in captured.err.lower() or "no converter" in captured.err.lower()
    assert out_docx.exists()
    assert not (tmp_path / "resume.pdf").exists()


def test_cli_format_docx_skips_pdf_path(monkeypatch, tmp_path):
    """Default --format docx (or omitted) should NEVER invoke conversion,
    even if a converter is installed. Guards against accidental Word
    launches on macOS where docx2pdf brings up the app."""
    from resume_builder import cli as _cli

    called = {"convert": False}

    def should_not_be_called(*a, **kw):
        called["convert"] = True
        raise AssertionError("convert_docx_to_pdf should not be called")

    monkeypatch.setattr(_cli, "available_converter_name", lambda: "libreoffice")
    monkeypatch.setattr(_cli, "convert_docx_to_pdf", should_not_be_called)

    master = FIXTURES / "sample-master.yaml"
    out_docx = tmp_path / "resume.docx"
    rc = _cli.main([
        "--master", str(master),
        "--out", str(out_docx),
        "--no-tailor",
    ])
    assert rc == 0
    assert not called["convert"]


# ---------- end-to-end (only runs if a real converter is installed) ----------


@pytest.mark.skipif(
    available_converter_name() is None,
    reason="No PDF converter (docx2pdf or libreoffice) available on this machine",
)
def test_real_conversion_produces_valid_pdf(tmp_path):
    """Drive the full pipeline: render a real .docx from the sample master,
    convert it to PDF, verify the PDF magic bytes + that PyPDF can parse it."""
    from resume_builder.loaders import load_master
    from resume_builder.render import render_docx
    from resume_builder.schema import Template

    master = load_master(FIXTURES / "sample-master.yaml")
    docx_path = tmp_path / "real.docx"
    render_docx(master, Template(), docx_path)

    pdf_path = tmp_path / "real.pdf"
    result = convert_docx_to_pdf(docx_path, pdf_path)
    assert result.pdf_path == pdf_path
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 1000

    # Magic bytes
    header = pdf_path.read_bytes()[:4]
    assert header == b"%PDF"

    # PyPDF can actually parse it
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf_path))
        assert len(reader.pages) >= 1
    except ImportError:
        pytest.skip("pypdf not installed — PDF parsability check skipped")
