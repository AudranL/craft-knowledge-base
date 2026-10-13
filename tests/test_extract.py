from craft_kb.chunking import chunk_section
from craft_kb.extract import extract_epub, extract_pdf

from .conftest import FILLER, make_epub, make_pdf


def test_epub_sections_follow_the_table_of_contents(tmp_path):
    book = extract_epub(make_epub(tmp_path / "b.epub"))

    assert book.title == "Domain Modeling Sample"
    assert book.authors == "Jane Evans"
    assert [s.title for s in book.sections] == [
        "Test-Driven Development",
        "Aggregates",
        "Invariants",
        "Designing small aggregates",
    ]
    assert book.sections[2].breadcrumb == ["Part II: Domain Model"]
    assert book.sections[2].location == "ch2.xhtml#invariants"


def test_epub_keeps_code_blocks_and_headings(tmp_path):
    tdd = extract_epub(make_epub(tmp_path / "b.epub")).sections[0].text

    assert "## Red, Green, Refactor" in tdd
    assert "```\n@Test\nvoid shouldRejectEmptyCart() {\n" in tdd


def test_pdf_sections_come_from_the_outline_with_page_ranges(tmp_path):
    book = extract_pdf(make_pdf(tmp_path / "r.pdf"))

    assert book.title == "Refactoring Handbook"
    assert [(s.title, s.breadcrumb, s.location) for s in book.sections] == [
        ("Refactoring", [], "p. 1"),
        ("Extract Method", ["Refactoring"], "p. 2"),
        ("Code Smells", [], "p. 3"),
    ]
    extract_method = book.sections[1].text
    assert extract_method.startswith("### Extract Method")  # titre reconnu à sa taille de police
    assert "```\nint total() {" in extract_method
    assert "Long Method" not in extract_method


def test_chunks_stay_bounded_and_carry_their_heading():
    text = "\n\n".join(
        ["## First", *[f"Para {i}. " + FILLER for i in range(12)], "## Second", *[FILLER] * 6]
    )
    chunks = chunk_section(text, "Chapter")

    assert len(chunks) > 2
    assert all(len(c.text.split()) <= 450 + 80 for c in chunks)
    assert chunks[0].heading == "First"
    assert chunks[-1].heading == "Second"
