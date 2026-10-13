from craft_kb.kb import KnowledgeBase, fts_query

from .conftest import FakeEmbedder


def test_reingesting_an_unchanged_library_is_a_no_op(kb, library):
    counts = kb.ingest_dir(library, log=lambda _: None)

    assert counts == {"inchangé": 2}


def test_full_text_search_finds_the_right_section(kb):
    result = kb.search("Extract Method")

    assert result.mode == "hybride"
    assert result.hits[0].book == "Refactoring Handbook"
    assert result.hits[0].section == "Refactoring"
    assert "### Extract Method" in result.hits[0].text


def test_semantic_search_bridges_french_queries_to_english_books(tmp_path, library):
    base = KnowledgeBase(tmp_path / "fts.sqlite", FakeEmbedder())
    base.ingest_dir(library, log=lambda _: None)
    assert base.search("frontière de cohérence agrégat").mode == "plein texte"

    base.embed_missing(log=lambda _: None)
    result = base.search("frontière de cohérence agrégat")

    assert result.mode == "sémantique"
    assert result.hits[0].book == "Domain Modeling Sample"
    assert result.hits[0].section.startswith("Part II: Domain Model")


def test_book_filter_restricts_results(kb):
    hits = kb.search("code", book="refactoring").hits

    assert hits and {h.book for h in hits} == {"Refactoring Handbook"}
    assert kb.search("code", book="introuvable").warning


def test_read_section_paginates_and_links_neighbours(kb):
    first = kb.search("Invariants").hits[0]
    page = kb.read_section(first.section_id, max_chars=250)

    assert page["next_offset"] is not None
    assert page["text"].split()[-1].isalpha() or page["text"].endswith(".")
    assert page["previous_section_id"] and page["next_section_id"]
    rest = kb.read_section(first.section_id, offset=page["next_offset"], max_chars=40000)
    assert rest["next_offset"] is None
    assert len(page["text"]) + len(rest["text"]) >= page["total_chars"] - 4


def test_search_without_embedder_falls_back_to_full_text(tmp_path, library):
    base = KnowledgeBase(tmp_path / "plain.sqlite")
    base.ingest_dir(library, log=lambda _: None)

    result = base.search("aggregate root invariants")

    assert result.mode == "plein texte"
    assert result.hits[0].section.endswith("Invariants")


def test_fts_query_drops_stopwords_and_quotes_terms():
    assert fts_query("Qu'est-ce que le TDD ?") == '"tdd"'
    assert fts_query("aggregate root, aggregate") == '"aggregate" OR "root"'
    assert fts_query("the") == '"the"'


def test_metadata_file_overrides_titles_without_reextracting(kb, library):
    (library / "craft-kb.toml").write_text(
        '["refactoring*.pdf"]\ntitle = "Refactoring (1st ed.)"\nauthors = "Martin Fowler"\n', encoding="utf-8"
    )

    counts = kb.ingest_dir(library, log=lambda _: None)

    assert counts == {"inchangé": 1, "métadonnées": 1}
    hit = kb.search("Extract Method", book="fowler").hits[0]
    assert (hit.book, hit.authors) == ("Refactoring (1st ed.)", "Martin Fowler")
