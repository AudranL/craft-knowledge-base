from __future__ import annotations

import re
import zlib
from pathlib import Path

import numpy as np
import pytest

from craft_kb.embed import normalize
from craft_kb.kb import KnowledgeBase

FILLER = (
    "Craftsmanship is about caring for the code we write every day and leaving it cleaner than we found it. "
    "Teams that practice this discipline deliver steadily because feedback loops stay short and design stays supple. "
)

SYNONYMS = {"agrégat": "aggregate", "agrégats": "aggregate", "frontière": "boundary", "cohérence": "consistency"}


class FakeEmbedder:
    """Sac de mots haché, avec quelques synonymes FR→EN pour simuler la sémantique."""

    model = "fake-embed"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        out = np.zeros((len(texts), 512), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"\w+", text.lower()):
                word = SYNONYMS.get(word, word)
                out[i, zlib.crc32(word.rstrip("s").encode()) % 512] += 1.0
        return normalize(out)


def make_epub(path: Path) -> Path:
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("ddd-sample")
    book.set_title("Domain Modeling Sample")
    book.set_language("en")
    book.add_author("Jane Evans")

    cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml", content="<h1>Domain Modeling</h1>")
    ch1 = epub.EpubHtml(title="Test-Driven Development", file_name="ch1.xhtml")
    ch1.content = f"""<h1>Test-Driven Development</h1>
        <p>Write a failing test first. {FILLER * 2}</p>
        <h2>Red, Green, Refactor</h2>
        <p>The cycle keeps steps tiny. {FILLER}</p>
        <pre>@Test
void shouldRejectEmptyCart() {{
    assertThrows(IllegalStateException.class, () -> cart.checkout());
}}</pre>
        <p>Triangulation adds a second example before generalising. {FILLER}</p>"""
    ch2 = epub.EpubHtml(title="Aggregates", file_name="ch2.xhtml")
    ch2.content = f"""<h1 id="aggregates">Aggregates</h1>
        <p>An aggregate is a cluster of objects treated as a unit for data changes. {FILLER}</p>
        <h2 id="invariants">Invariants</h2>
        <p>The aggregate root enforces invariants and is the consistency boundary of the cluster. {FILLER * 2}</p>
        <h2 id="size">Designing small aggregates</h2>
        <p>Prefer small aggregates and reference other aggregates by identity. {FILLER * 2}</p>"""
    for item in (cover, ch1, ch2):
        book.add_item(item)
    book.toc = [
        epub.Link("cover.xhtml", "Cover", "cover"),
        epub.Link("ch1.xhtml", "Test-Driven Development", "ch1"),
        (
            epub.Section("Part II: Domain Model"),
            [
                epub.Link("ch2.xhtml", "Aggregates", "ch2"),
                epub.Link("ch2.xhtml#invariants", "Invariants", "inv"),
                epub.Link("ch2.xhtml#size", "Designing small aggregates", "size"),
            ],
        ),
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", cover, ch1, ch2]
    epub.write_epub(str(path), book)
    return path


def make_pdf(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    pages = [
        ("Refactoring", "Refactoring is changing the structure of code without changing its behaviour. " + FILLER * 3, None),
        ("Extract Method", "Extract Method turns a fragment of code into a method whose name explains its purpose. " + FILLER * 2,
         "int total() {\n  return items.stream()\n    .mapToInt(Item::price).sum();\n}"),
        ("Code Smells", "Long Method and Feature Envy are smells that signal a refactoring opportunity. " + FILLER * 3, None),
    ]
    for i, (title, body, code) in enumerate(pages, 1):
        page = doc.new_page()
        page.insert_text((72, 40), "Refactoring Handbook", fontname="helv", fontsize=8)
        page.insert_textbox(pymupdf.Rect(72, 70, 540, 100), title, fontname="hebo", fontsize=16)
        page.insert_textbox(pymupdf.Rect(72, 110, 540, 500), body, fontname="helv", fontsize=11)
        if code:
            page.insert_textbox(pymupdf.Rect(72, 520, 540, 620), code, fontname="cour", fontsize=10)
        page.insert_text((300, 800), str(i), fontname="helv", fontsize=8)
    doc.set_toc([[1, "Refactoring", 1], [2, "Extract Method", 2], [1, "Code Smells", 3]])
    doc.set_metadata({"title": "Refactoring Handbook", "author": "Martin Sample"})
    doc.save(str(path))
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    folder = tmp_path / "ebooks"
    folder.mkdir()
    make_epub(folder / "domain.epub")
    make_pdf(folder / "refactoring.pdf")
    return folder


@pytest.fixture
def kb(tmp_path: Path, library: Path) -> KnowledgeBase:
    base = KnowledgeBase(tmp_path / "kb.sqlite", FakeEmbedder())
    base.ingest_dir(library, log=lambda _: None)
    base.embed_missing(log=lambda _: None)
    return base
