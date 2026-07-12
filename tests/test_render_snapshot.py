"""Snapshot-style tests on rendered .docx output."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from resume_builder.loaders import load_master, load_template
from resume_builder.render import render_docx
from resume_builder.schema import (
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def master():
    return load_master(FIXTURES / "sample-master.yaml")


@pytest.fixture
def template():
    return load_template(FIXTURES / "sample-template.yaml")


def _doc_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


def _footer_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        footer_names = [n for n in z.namelist() if n.startswith("word/footer")]
        return "\n".join(z.read(n).decode("utf-8") for n in footer_names)


def test_render_master_only(master, template, tmp_path):
    out = render_docx(master, template, tmp_path / "raw.docx")
    assert out.exists()
    xml = _doc_xml(out)
    # Body content
    assert "Jane Doe" in xml
    assert "Acme Logistics" in xml
    assert "Bolt Analytics" in xml
    assert "pgwatch-lite" in xml
    assert "University of Michigan" in xml
    # Section headings
    assert "EXPERIENCE" in xml
    assert "PROJECTS" in xml
    assert "EDUCATION" in xml
    assert "SKILLS" in xml
    # Font and margin styling applied
    assert "Calibri" in xml
    assert "pgMar" in xml  # page margins
    assert "pBdr" in xml  # heading underline border
    footer = _footer_xml(out)
    assert "Jane Doe" in footer
    assert "jane@example.com" in footer
    assert "PAGE" in footer


def test_render_with_tailored_filters_and_reorders(master, template, tmp_path):
    # Tailored output: only show acme job, only its bullets 4 then 1, and only the project.
    tailored = TailoredResume(
        summary="Senior backend engineer focused on Kubernetes and Postgres.",
        sections=[
            TailoredSection(
                name="experience",
                items=[
                    TailoredItem(
                        source_id="exp-acme",
                        bullets=[
                            TailoredBullet(
                                source_id="exp-acme-4",
                                rewritten_text="Built a Kubernetes operator that cut deploy time from 18 minutes to 4 minutes.",
                            ),
                            TailoredBullet(
                                source_id="exp-acme-2",
                                rewritten_text="Designed Postgres partitioning saving $40K/year in storage.",
                            ),
                        ],
                    ),
                ],
            ),
            TailoredSection(
                name="projects",
                items=[
                    TailoredItem(
                        source_id="proj-pgwatch",
                        bullets=[
                            TailoredBullet(
                                source_id="proj-pgwatch-1",
                                rewritten_text="Open-source Postgres dashboard, used in production at 8 companies.",
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    out = render_docx(master, template, tmp_path / "tailored.docx", tailored=tailored)
    xml = _doc_xml(out)

    # Tailored summary used
    assert "Senior backend engineer focused on Kubernetes" in xml
    # Acme rendered, Bolt and Coda dropped
    assert "Acme Logistics" in xml
    assert "Bolt Analytics" not in xml
    assert "Coda Tools" not in xml
    # Bullet text reflects rewritten versions
    assert "cut deploy time from 18 minutes to 4 minutes" in xml
    # Project rendered
    assert "pgwatch-lite" in xml


def test_section_order_respected(master, template, tmp_path):
    # Move skills above experience
    template.section_order = [
        "summary",
        "skills",
        "experience",
        "projects",
        "education",
    ]
    out = render_docx(master, template, tmp_path / "reordered.docx")
    xml = _doc_xml(out)
    skills_idx = xml.index("SKILLS")
    exp_idx = xml.index("EXPERIENCE")
    assert skills_idx < exp_idx, "Skills should appear before Experience after reorder"
