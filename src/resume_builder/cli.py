"""Command-line entry point: master + JD + pointers -> tailored .docx.

No Anthropic API key required. Tailoring uses either:
- `claude -p` (Claude Code headless), via your existing Claude Code login, OR
- copy-paste mode: write the prompt to a file, paste into any Claude session,
  drop the JSON reply back in a file, and re-render.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .ats import ATSReport, score_ats
from .file_extract import extract_text
from .formatting_check import FormattingWarning, check_template
from .guard import GuardResult, validate
from .jd_signals import extract as extract_jd_signals
from .lints import LintWarning, lint
from .loaders import (
    apply_auto_length,
    load_jd_text,
    load_master,
    load_pointers,
    load_tailored_json,
    load_template,
    pointers_from_dict,
    save_tailored_json,
)
from .pdf_export import (
    ConversionError,
    LastResortError,
    available_converter_name,
    convert_docx_to_pdf,
)
from .render import render_docx
from .template_presets import PRESETS, default_preset_id, get_preset
from .schema import Pointers
from .tailor import (
    DEFAULT_MODEL,
    ClaudeCliError,
    parse_response_file,
    tailor_via_claude_cli,
    write_prompt_for_paste,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="resume-builder",
        description="Tailor a YAML master resume to a job description and emit a formatted .docx. Uses Claude Code, no Anthropic API key required.",
    )
    p.add_argument("--master", required=True, help="Path to master resume YAML")
    p.add_argument(
        "--out",
        default=None,
        help="Output .docx path (required unless --print-prompt)",
    )
    p.add_argument(
        "--template", default=None, help="Path to template.yaml (uses defaults if omitted)"
    )
    p.add_argument(
        "--pointers", default=None, help="Path to pointers.yaml (CLI flags override)"
    )

    # JD input
    jd_group = p.add_mutually_exclusive_group()
    jd_group.add_argument("--jd", help="Path to a JD text file")
    jd_group.add_argument("--jd-text", help="JD text inline")

    # Pointer overrides
    p.add_argument("--length", help="1page | 2page | <int> word count")
    p.add_argument(
        "--seniority",
        choices=["ic", "senior", "staff", "manager", "founding-eng"],
    )
    p.add_argument(
        "--must-include",
        help="Comma-separated keywords to force-surface (when source supports it)",
    )
    p.add_argument(
        "--context",
        choices=["startup", "faang", "consulting", "nonprofit", "research"],
    )
    p.add_argument(
        "--extra-instructions",
        help="Freeform style/emphasis guidance for the tailor "
             '(e.g. "emphasize leadership, British English"). '
             "Style only — the no-invention guard still applies.",
    )

    # Tailoring control — pick one of these (default: try claude CLI)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--no-tailor",
        action="store_true",
        help="Skip tailoring entirely. Render the master verbatim.",
    )
    mode.add_argument(
        "--print-prompt",
        metavar="FILE",
        help="Write the LLM prompt to FILE and exit. Paste it into any Claude session, save Claude's JSON reply, then re-run with --from-response.",
    )
    mode.add_argument(
        "--from-response",
        metavar="FILE",
        help="Read a previously-collected Claude JSON response from FILE. Skips the LLM call.",
    )
    mode.add_argument(
        "--tailored-json",
        default=None,
        help="Re-render from a previously-saved tailored.json (already validated by the guard).",
    )

    p.add_argument(
        "--save-tailored",
        default=None,
        help="If set, save the tailored output to this JSON path before rendering.",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Claude model alias for `claude -p` (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if the no-invention guard fires any warnings.",
    )
    p.add_argument(
        "--strict-bock",
        action="store_true",
        help="Exit non-zero if Bock formatting checks fail.",
    )

    # Phase 8: PDF export. Default is docx-only. When --format pdf is set,
    # we render the .docx as usual then convert to PDF alongside.
    p.add_argument(
        "--format",
        choices=["docx", "pdf"],
        default="docx",
        help=(
            "Output format. 'docx' (default) writes only the .docx. "
            "'pdf' also writes a .pdf next to it (requires Word or LibreOffice). "
            "Bock recommends PDF for ATS submissions."
        ),
    )

    # Phase 10 item 1: named template preset. Overrides --template when set.
    p.add_argument(
        "--preset",
        choices=[p_.id for p_ in PRESETS],
        default=None,
        help=(
            "Pick a named format preset (overrides --template). "
            f"Options: {', '.join(p_.id for p_ in PRESETS)}. "
            "Each preset bundles page size, fonts, spacing, colors, and a "
            "matching length pointer (1page or 2page) for the tailor."
        ),
    )

    return p


def _merge_pointers(file_pointers: Pointers, args: argparse.Namespace) -> Pointers:
    return pointers_from_dict(
        file_pointers,
        {
            "length": args.length,
            "seniority": args.seniority,
            "context": args.context,
            "must_include": args.must_include,
            "extra_instructions": args.extra_instructions,
        },
    )


def _resolve_jd_text(args: argparse.Namespace) -> Optional[str]:
    if args.jd:
        return load_jd_text(args.jd)
    if args.jd_text:
        return args.jd_text.strip()
    return None


def _print_warnings(guard: GuardResult) -> None:
    if not guard.warnings:
        return
    print(
        f"\n[no-invention guard] {len(guard.warnings)} bullet(s) dropped:",
        file=sys.stderr,
    )
    for w in guard.warnings:
        print(f"  - {w.bullet_source_id}: {w.reason}", file=sys.stderr)
        snippet = w.rewritten_text.strip().replace("\n", " ")
        if len(snippet) > 140:
            snippet = snippet[:137] + "..."
        print(f"    rewritten: {snippet}", file=sys.stderr)


def _print_lints(lints: list[LintWarning]) -> None:
    if not lints:
        return
    print(f"\n[style lints] {len(lints)} suggestion(s):", file=sys.stderr)
    for w in lints:
        sid = f" [{w.source_id}]" if w.source_id else ""
        print(f"  - ({w.rule}){sid} {w.message}", file=sys.stderr)
        if w.snippet:
            snip = w.snippet.strip().replace("\n", " ")
            if len(snip) > 140:
                snip = snip[:137] + "..."
            print(f"    text: {snip}", file=sys.stderr)


def _print_formatting(warnings: list[FormattingWarning]) -> None:
    if not warnings:
        return
    print(f"\n[bock formatting] {len(warnings)} warning(s):", file=sys.stderr)
    for w in warnings:
        print(f"  - ({w.rule}) {w.message}", file=sys.stderr)


def _print_ats(report: ATSReport) -> None:
    pct = int(report.score * 100)
    print(
        f"\n[ats] {pct}% keyword coverage "
        f"({len(report.matched)}/{report.total_checked}, {report.word_count} words rendered)",
        file=sys.stderr,
    )
    if report.missing:
        missing = ", ".join(report.missing[:8])
        if len(report.missing) > 8:
            missing += f", … (+{len(report.missing) - 8} more)"
        print(f"  missing: {missing}", file=sys.stderr)
    for w in report.warnings:
        print(f"  ({w.rule}) {w.message}", file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    master = load_master(args.master)
    # If --preset is set, it overrides --template and seeds a length pointer.
    if args.preset:
        preset = get_preset(args.preset)
        template = preset.template
        if not args.length:
            args.length = preset.length_pointer
    else:
        template = load_template(args.template)
    pointers = apply_auto_length(master, _merge_pointers(load_pointers(args.pointers), args))
    formatting_warnings = check_template(template)
    _print_formatting(formatting_warnings)

    # ---- Sub-command: write prompt and exit ----
    if args.print_prompt:
        jd_text = _resolve_jd_text(args)
        if jd_text is None:
            parser.error("--print-prompt requires --jd or --jd-text")
        path = write_prompt_for_paste(master, jd_text, pointers, args.print_prompt)
        print(f"[prompt] wrote {path}", file=sys.stderr)
        print(
            "Next: paste it into a Claude conversation, save the JSON reply,\n"
            "then re-run with `--from-response <that-file> --out <out.docx>`.",
            file=sys.stderr,
        )
        return 0

    # All remaining paths require --out
    if not args.out:
        parser.error("--out is required (omit only with --print-prompt)")

    tailored = None
    warnings_count = 0

    if args.no_tailor:
        if args.jd or args.jd_text:
            print(
                "[note] --no-tailor set; ignoring JD input and rendering master verbatim.",
                file=sys.stderr,
            )
    elif args.tailored_json:
        # Re-render from a previously cleaned tailored.json. Re-run guard if JD given.
        tailored = load_tailored_json(args.tailored_json)
        jd_text = _resolve_jd_text(args)
        if jd_text is None:
            print(
                "[note] --tailored-json without JD: skipping guard validation.",
                file=sys.stderr,
            )
        else:
            guard = validate(master, tailored, jd_text, pointers=pointers)
            _print_warnings(guard)
            warnings_count = len(guard.warnings)
            tailored = guard.cleaned
    elif args.from_response:
        jd_text = _resolve_jd_text(args)
        if jd_text is None:
            parser.error("--from-response requires --jd or --jd-text for guard validation")
        try:
            raw_tailored = parse_response_file(args.from_response)
        except Exception as e:
            print(f"[error] failed to parse {args.from_response}: {e}", file=sys.stderr)
            return 2
        if args.save_tailored:
            save_tailored_json(raw_tailored, args.save_tailored)
        guard = validate(master, raw_tailored, jd_text, pointers=pointers)
        _print_warnings(guard)
        warnings_count = len(guard.warnings)
        tailored = guard.cleaned
    else:
        # Default path: shell out to `claude -p`
        jd_text = _resolve_jd_text(args)
        if jd_text is None:
            parser.error(
                "Provide --jd or --jd-text (or use --no-tailor / --tailored-json / --from-response / --print-prompt)."
            )

        print(f"[tailor] calling `claude -p` (model: {args.model})...", file=sys.stderr)
        try:
            raw_tailored = tailor_via_claude_cli(master, jd_text, pointers, model=args.model)
        except ClaudeCliError as e:
            print(f"[error] {e}", file=sys.stderr)
            print(
                "\nTip: if `claude` isn't authenticated in this terminal, use copy-paste mode:\n"
                "  python -m resume_builder ... --print-prompt prompt.txt\n"
                "  # paste prompt.txt into any Claude session, save the JSON reply\n"
                "  python -m resume_builder ... --from-response reply.json --out out.docx",
                file=sys.stderr,
            )
            return 2

        if args.save_tailored:
            save_tailored_json(raw_tailored, args.save_tailored)
            print(f"[tailor] saved raw output to {args.save_tailored}", file=sys.stderr)

        guard = validate(master, raw_tailored, jd_text, pointers=pointers)
        _print_warnings(guard)
        warnings_count = len(guard.warnings)
        tailored = guard.cleaned

    out = render_docx(master, template, args.out, tailored=tailored)
    print(f"[render] wrote {out}", file=sys.stderr)

    if args.format == "pdf":
        pdf_out = Path(out).with_suffix(".pdf")
        converter = available_converter_name()
        if converter is None:
            print(
                "[pdf] no converter available — keeping .docx only.\n"
                "      Install Microsoft Word (macOS/Windows) or LibreOffice\n"
                "      (cross-platform), or open the .docx and File → Save As → PDF.",
                file=sys.stderr,
            )
        else:
            try:
                result = convert_docx_to_pdf(out, pdf_out)
                print(f"[pdf] wrote {result.pdf_path} (via {result.converter})",
                      file=sys.stderr)
            except LastResortError as e:
                print(f"[pdf] {e}", file=sys.stderr)
            except ConversionError as e:
                print(f"[pdf] conversion failed: {e}", file=sys.stderr)

    if tailored is not None:
        lints_out = lint(tailored, pointers=pointers, master=master)
        _print_lints(lints_out)

        # ATS keyword coverage: only meaningful when we have a JD or must-include.
        jd_text = _resolve_jd_text(args)
        if jd_text or pointers.must_include:
            try:
                with open(out, "rb") as f:
                    docx_text, _ = extract_text("rendered.docx", f.read())
                signals = extract_jd_signals(jd_text) if jd_text else None
                ats_report = score_ats(docx_text, signals=signals, pointers=pointers)
                _print_ats(ats_report)
            except Exception as e:  # noqa: BLE001 — advisory; never fail the run
                print(f"[ats] skipped: {e}", file=sys.stderr)

    if args.strict_bock and formatting_warnings:
        return 4
    if args.strict and warnings_count:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
