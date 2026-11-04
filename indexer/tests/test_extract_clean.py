"""Stages 1 and 2: extraction (text layer, tables, OCR, HTML, Markdown) and cleaning."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pipeline.clean import clean, normalise_text, repeated_edge_keys
from pipeline.extract import extract, html_to_text, render_table
from pipeline.models import Document, Page

from tests.pdfmaker import scanned_pdf, table_pdf, text_pdf

needs_ocr = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
    reason="tesseract and poppler are installed in the indexer image and in CI",
)


# ------------------------------------------------------------------------- extract
def test_text_pdf_keeps_page_numbers(tmp_path: Path) -> None:
    pdf = text_pdf(tmp_path / "rules.pdf", [["1. Attendance", "Students attend 80 percent of lectures."],
                                            ["2. Fees", "Tuition is charged per credit hour."]])
    doc = extract(str(pdf), "2026", False)
    assert [p.page for p in doc.pages] == [1, 2]
    assert "80 percent" in doc.pages[0].text and "credit hour" in doc.pages[1].text
    assert not any(p.ocr for p in doc.pages) and doc.problems == []
    assert (doc.source, doc.doc_version, doc.superseded) == ("rules.pdf", "2026", False)


def test_a_ruled_table_keeps_its_columns(tmp_path: Path) -> None:
    pdf = table_pdf(tmp_path / "fees.pdf", "Fee schedule for Fall 2026",
                    [["Charge", "Amount (PKR)"], ["Late registration", "3,000"], ["Transcript", "1,500"]])
    text = extract(str(pdf), "2026", False).pages[0].text
    assert "Fee schedule" in text
    assert "Late registration | 3,000" in text and "Transcript | 1,500" in text
    assert "Charge | Amount (PKR)" in text


@needs_ocr
def test_scanned_pdf_is_ocrd_and_marked(tmp_path: Path) -> None:
    pdf = scanned_pdf(tmp_path / "scan.pdf", ["Faculty Entrepreneurship Policy", "Section 4 Start-up Leave"])
    doc = extract(str(pdf), "2022-05", False)
    assert doc.pages[0].ocr is True
    text = doc.pages[0].text.lower()
    assert "entrepreneurship" in text and "leave" in text
    assert doc.problems == []


def test_without_ocr_a_scan_is_reported_not_silently_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure stage A (docs/FAILURE-ANALYSIS.md): the page comes out empty and says so."""
    pytest.importorskip("PIL")
    monkeypatch.setenv("OCR_ENABLED", "false")
    pdf = scanned_pdf(tmp_path / "scan.pdf", ["Some scanned words here"])
    doc = extract(str(pdf), "1", False)
    assert doc.pages[0].text.strip() == "" and doc.pages[0].ocr is False
    assert any("no text layer" in p for p in doc.problems)


def test_ocr_failure_is_recorded_as_a_problem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL")
    import pytesseract

    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("tesseract crashed")

    monkeypatch.setattr(pytesseract, "image_to_string", boom)
    if shutil.which("pdftoppm") is None:
        pytest.skip("poppler not installed")
    doc = extract(str(scanned_pdf(tmp_path / "s.pdf", ["words"])), "1", False)
    assert any("OCR failed" in p for p in doc.problems)


def test_markdown_front_matter_is_stripped(tmp_path: Path) -> None:
    md = tmp_path / "a.md"
    md.write_text("---\nsource: a.md\ndoc_version: 2026\n---\n# Title\n\nBody text.\n", encoding="utf-8")
    doc = extract(str(md), "2026", False)
    assert doc.pages[0].text.startswith("# Title") and "doc_version" not in doc.pages[0].text


def test_html_keeps_headings_and_table_rows_and_drops_navigation(tmp_path: Path) -> None:
    html = tmp_path / "fees.html"
    html.write_text(
        "<html><head><title>x</title><script>var a=1;</script></head><body><nav>Home About</nav>"
        "<h2>Tuition</h2><p>Fees per credit hour.</p><table><tr><th>Programme</th><th>PKR</th></tr>"
        "<tr><td>BS</td><td>10,000</td></tr></table><footer>Copyright</footer></body></html>",
        encoding="utf-8")
    text = extract(str(html), "2026", False).pages[0].text
    assert "## Tuition" in text and "BS | 10,000" in text and "Programme | PKR" in text
    assert "Home About" not in text and "var a" not in text and "Copyright" not in text


def test_render_table_collapses_cell_newlines_and_skips_empty_rows() -> None:
    rows = [["a\nb", "x"], [None, None], ["c", "d"]]
    assert render_table(rows) == "a b | x\nc | d"


def test_render_table_keeps_real_empty_cells_so_columns_stay_aligned() -> None:
    rows = [["Charge", "Amount", "Note"], ["Late", "3,000", ""], ["Transcript", "1,500", "per copy"]]
    assert render_table(rows) == "Charge | Amount | Note\nLate | 3,000 | \nTranscript | 1,500 | per copy"


def test_render_table_squeezes_the_empty_cells_that_merged_cells_leave_behind() -> None:
    rows = [["Campus", "", "", "Prof.", "", "", "Total"], ["", "Lahore", "", "", "16", "", "188"]]
    assert render_table(rows) == "Campus | Prof. | Total\nLahore | 16 | 188"


def test_unsupported_file_type_is_an_error(tmp_path: Path) -> None:
    f = tmp_path / "x.xyz"
    f.write_text("hi")
    with pytest.raises(ValueError):
        extract(str(f), "1", False)


def test_html_to_text_handles_empty_input() -> None:
    assert html_to_text("") == ""


# --------------------------------------------------------------------------- clean
def _doc(pages: list[str]) -> Document:
    return Document("d.pdf", "2026", False, [Page("d.pdf", i + 1, t) for i, t in enumerate(pages)])


def test_repeating_headers_and_footers_are_removed_but_body_text_stays() -> None:
    pages = [f"FAST-NUCES Academic Rules 2026\n5.4 Academic Warning\nBody sentence {i}.\nPage {i} of 6"
             for i in range(1, 7)]
    out = clean(_doc(pages))
    for page in out.pages:
        assert "FAST-NUCES Academic Rules" not in page.text and "Page " not in page.text
        assert "Body sentence" in page.text


def test_headings_that_appear_once_are_not_treated_as_headers() -> None:
    pages = ["Running header\n1. Attendance\nText A", "Running header\n2. Warning\nText B",
             "Running header\n3. Fees\nText C", "Running header\n4. Refunds\nText D"]
    out = clean(_doc(pages))
    assert [p.text.split("\n")[0] for p in out.pages] == ["1. Attendance", "2. Warning", "3. Fees", "4. Refunds"]


def test_bare_page_numbers_at_the_page_edge_are_removed_but_numbers_in_tables_are_not() -> None:
    pages = ["Intro text\nCharge | Amount\nLate | 3,000\n12", "More text\nTranscript | 1,500\n13"]
    out = clean(_doc(pages))
    assert out.pages[0].text.endswith("Late | 3,000")
    assert "Transcript | 1,500" in out.pages[1].text


def test_table_rows_survive_cleaning_even_when_they_repeat() -> None:
    row = "Total | 100"
    out = clean(_doc([f"Text {i}\n{row}" for i in range(6)]))
    assert all(row in p.text for p in out.pages)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("exam-\nination rules", "examination rules"),
        ("COVID-\n19 rules", "COVID-\n19 rules"),
        ("a   b\t\tc", "a b c"),
        ("ﬁnal ﬂow", "final flow"),
        ("Fees ........ 12", "Fees 12"),
        ("one\n\n\n\n\ntwo", "one\n\ntwo"),
        ("soft­hyphen and zero​width", "softhyphen and zerowidth"),
    ],
)
def test_normalise_text(raw: str, expected: str) -> None:
    assert normalise_text(raw) == expected


def test_too_few_pages_never_count_as_repeating() -> None:
    assert repeated_edge_keys([["Header", "a"], ["Header", "b"]]) == set()


def test_clean_keeps_ocr_flag_problems_and_metadata() -> None:
    doc = Document("s.pdf", "2022-05", True, [Page("s.pdf", 1, "Text  here", ocr=True)], ["note"])
    out = clean(doc)
    assert out.pages[0].ocr and out.problems == ["note"] and out.superseded and out.doc_version == "2022-05"
