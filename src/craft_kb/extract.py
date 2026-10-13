"""Extraction des ebooks (EPUB, PDF) en sections structurées.

Chaque livre devient une liste de sections (chapitres ou sous-chapitres),
rendues en Markdown léger : titres `##`, blocs de code ``` et paragraphes.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

MIN_SECTION_WORDS = 30
LARGE_SECTION_WORDS = 5000  # profondeur de sommaire : 90 % des sections sous ce seuil
SPLIT_SECTION_WORDS = 6000  # au-delà, on redécoupe la section sur ses titres internes
PDF_FALLBACK_PAGES = 10
_WS = re.compile(r"\s+")

# Pages liminaires et annexes sans valeur pour la recherche.
_SKIPPED_TITLE = re.compile(
    r"^(title ?page|half ?title|main page|(front |back )?cover|copyright( page)?|dedication( page)?"
    r"|(brief |table of )?contents?|table of content|about this e-?book|praise for\b.*"
    r"|acknowledge?ments?|about the cover( illustration)?|on the cover|about the authors?"
    r"|also by\b.*|other books\b.*|titles in the series|the [\w ]+ series|colophon"
    r"|index( of terms)?|index des notions|table des matières|remerciements|sommaire)$",
    re.I,
)
_LEANPUB_PROGRESS = re.compile(r"\s+-\s+\d{1,3}\s?%.*$")
_HEADING_LINE = re.compile(r"^(#{2,6}) (.+)$", re.M)


@dataclass
class Section:
    title: str
    breadcrumb: list[str]
    location: str
    text: str


@dataclass
class Book:
    title: str
    authors: str
    path: Path
    format: str
    sections: list[Section] = field(default_factory=list)


@dataclass
class Block:
    kind: str  # "para" | "heading" | "code" | "anchor"
    text: str
    level: int = 0


def extract_book(path: Path) -> Book:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return extract_epub(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    raise ValueError(f"Format non supporté : {path.name}")


def render_blocks(blocks: list[Block]) -> str:
    parts: list[str] = []
    for b in blocks:
        if b.kind == "heading":
            parts.append(f"{'#' * min(max(b.level, 2), 6)} {b.text}")
        elif b.kind == "code":
            parts.append(f"```\n{b.text.rstrip()}\n```")
        elif b.kind == "para":
            parts.append(b.text)
    return "\n\n".join(parts)


def _word_count(text: str) -> int:
    return len(text.split())


# ------------------------------------------------------------- post-traitement

def clean_title(title: str | None, path: Path) -> str:
    """Titre lisible : métadonnée si elle est exploitable, sinon nom de fichier nettoyé."""
    t = _WS.sub(" ", title or "").strip()
    t = re.sub(r"\s+@\S.*$", "", t)  # signatures de diffusion (« @Team LiB »…)
    if not t or t.lower().endswith((".pdf", ".epub")) or t == path.stem or re.fullmatch(r"[\d\W_]+", t):
        stem = re.sub(r"^[0-9a-f]{8}-", "", path.stem)
        t = _WS.sub(" ", re.sub(r"[._]+", " ", stem)).strip()
    return t


def clean_authors(authors: str | None) -> str:
    a = _WS.sub(" ", authors or "").strip()
    a = re.sub(r"^by\s+", "", a, flags=re.I)
    if re.search(r"https?:|www\.|@|\.(com|ru|org|net)\b", a, re.I):
        return ""
    return a


def _clean_section_title(title: str) -> str:
    return _LEANPUB_PROGRESS.sub("", _WS.sub(" ", title).strip())


def _is_skipped(sec: Section) -> bool:
    return any(_SKIPPED_TITLE.match(t) for t in [*sec.breadcrumb, sec.title])


def _looks_like_toc(text: str) -> bool:
    """Table des matières ou index : surtout des lignes courtes terminées par un numéro de page."""
    prose = re.sub(r"```.*?```", "", text, flags=re.S)
    paras = [p.strip() for p in prose.split("\n\n") if p.strip()]
    if len(paras) < 25:
        return False
    paged = sum(1 for p in paras if len(p.split()) <= 12 and re.search(r"(\d+|[ivxlc]+|\.\s?\.\s?\.)\s*$", p))
    return paged / len(paras) >= 0.6


def finalize_sections(sections: list[Section]) -> list[Section]:
    """Nettoie les titres, écarte les pages liminaires et fusionne les sections trop courtes."""
    out: list[Section] = []
    carry = ""
    for sec in sections:
        sec = Section(
            _clean_section_title(sec.title), [_clean_section_title(t) for t in sec.breadcrumb], sec.location, sec.text
        )
        if _is_skipped(sec) or _looks_like_toc(sec.text):
            continue
        text = f"{carry}\n\n{sec.text}".strip() if carry else sec.text
        if _word_count(text) < MIN_SECTION_WORDS:
            carry = text  # trop court seul (titre de partie…) : rattaché à la section suivante
            continue
        carry = ""
        out.append(Section(sec.title, sec.breadcrumb, sec.location, text))
    if carry and out:
        last = out[-1]
        out[-1] = Section(last.title, last.breadcrumb, last.location, f"{last.text}\n\n{carry}")
    return out


def _fence_ranges(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"```.*?```", text, re.S)]


def _split_large(sec: Section, locate) -> list[Section]:
    """Redécoupe une section très longue sur ses titres internes du niveau le plus haut répété.

    `locate(start, end)` renvoie la localisation d'un sous-intervalle du texte de la section.
    """
    if _word_count(sec.text) <= SPLIT_SECTION_WORDS:
        return [sec]
    fences = _fence_ranges(sec.text)
    heads = [
        m for m in _HEADING_LINE.finditer(sec.text)
        if m.start() > 0 and not any(a <= m.start() < b for a, b in fences)
    ]
    levels = Counter(len(m.group(1)) for m in heads)
    level = min((lvl for lvl, n in levels.items() if n >= 2), default=None)
    if level is None:
        return [sec]
    cuts = [m for m in heads if len(m.group(1)) == level]
    parts: list[Section] = []
    intro = sec.text[: cuts[0].start()].strip()
    if intro:
        parts.append(Section(sec.title, sec.breadcrumb, locate(0, cuts[0].start()), intro))
    crumbs = [*sec.breadcrumb, sec.title]
    for i, m in enumerate(cuts):
        end = cuts[i + 1].start() if i + 1 < len(cuts) else len(sec.text)
        parts.append(Section(m.group(2).strip(), crumbs, locate(m.start(), end), sec.text[m.start():end].strip()))
    return parts


# --------------------------------------------------------------------------- EPUB

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = {
    "p", "div", "li", "ul", "ol", "blockquote", "section", "article", "aside",
    "table", "tr", "td", "th", "dd", "dt", "dl", "figure", "figcaption",
    "header", "footer", "body", "hr",
}
_SKIP = {"script", "style", "head", "title", "nav"}


def html_to_blocks(html: bytes | str, anchor_ids: set[str] | None = None) -> list[Block]:
    """Transforme un document XHTML en blocs (paragraphes, titres, code, ancres)."""
    import warnings

    from bs4 import BeautifulSoup, Comment, NavigableString, Tag, XMLParsedAsHTMLWarning

    with warnings.catch_warnings():  # le XHTML des EPUB se parse très bien en HTML
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    root = soup.body or soup
    anchors = anchor_ids or set()
    out: list[Block] = []
    buf: list[str] = []

    def flush() -> None:
        text = _WS.sub(" ", "".join(buf)).strip()
        buf.clear()
        if text:
            out.append(Block("para", text))

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                buf.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in _SKIP:
                continue
            if child.get("id") in anchors:
                flush()
                out.append(Block("anchor", child["id"]))
            if name == "pre":
                flush()
                code = child.get_text()
                if code.strip():
                    out.append(Block("code", code.strip("\n")))
            elif name in _HEADINGS:
                flush()
                text = _WS.sub(" ", child.get_text()).strip()
                if text:
                    out.append(Block("heading", text, int(name[1])))
            elif name == "br":
                buf.append(" ")
            elif name in _BLOCKS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    walk(root)
    flush()
    return out


def _norm_href(href: str) -> tuple[str, str]:
    href = unquote(href)
    path, _, frag = href.partition("#")
    return posixpath.normpath(path) if path else "", frag


def _flatten_toc(toc, parents: tuple[str, ...] = ()):
    for entry in toc:
        if isinstance(entry, (tuple, list)):
            head, children = entry[0], entry[1]
            href = getattr(head, "href", None)
            if href:
                yield href, head.title, parents
            yield from _flatten_toc(children, parents + (head.title,))
        else:
            yield entry.href, entry.title, parents


def extract_epub(path: Path) -> Book:
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(str(path), options={"ignore_ncx": False})

    def meta(name: str) -> list[str]:
        return [m[0] for m in book.get_metadata("DC", name) if m and m[0]]

    title = (meta("title") or [path.stem])[0]
    authors = ", ".join(meta("creator"))

    # Index de la table des matières : fichier -> [(fragment, titre, parents)]
    toc_by_file: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    for href, entry_title, parents in _flatten_toc(book.toc):
        file, frag = _norm_href(href)
        toc_by_file.setdefault(file, []).append((frag, entry_title, parents))

    def toc_entries(name: str):
        name = posixpath.normpath(name)
        if name in toc_by_file:
            return toc_by_file[name]
        for key, value in toc_by_file.items():  # chemins relatifs différents
            if key and (name.endswith("/" + key) or key.endswith("/" + name)):
                return value
        return []

    sections: list[dict] = []
    current: dict | None = None

    def start(sec_title: str, parents: tuple[str, ...], location: str) -> dict:
        nonlocal current
        current = {"title": sec_title, "breadcrumb": [*parents], "location": location, "blocks": []}
        sections.append(current)
        return current

    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        if isinstance(item, epub.EpubNav) or "nav" in (getattr(item, "properties", None) or []):
            continue
        name = item.get_name()
        entries = toc_entries(name)
        # Entrée la plus spécifique pointant sur le fichier entier (une « Partie » et son
        # premier chapitre pointent souvent vers le même fichier).
        wholes = [e for e in entries if not e[0]]
        whole = max(wholes, key=lambda e: len(e[2])) if wholes else None
        # On ne découpe sur les ancres qu'un niveau sous l'entrée la plus haute du fichier.
        top = min((len(p) for _, _, p in entries), default=0)
        anchors = {frag: (t, p) for frag, t, p in entries if frag and len(p) <= top + 1}
        blocks = html_to_blocks(item.get_content(), set(anchors))
        location = posixpath.basename(name)

        if whole:
            start(whole[1], whole[2], location)
        elif current is None or (blocks and blocks[0].kind == "heading" and blocks[0].level <= 2):
            first_heading = next((b.text for b in blocks if b.kind == "heading"), None)
            start(first_heading or Path(name).stem, (), location)

        for block in blocks:
            if block.kind == "anchor":
                sec_title, parents = anchors[block.text]
                start(sec_title, parents, f"{location}#{block.text}")
            else:
                current["blocks"].append(block)

    result = [Section(s["title"].strip(), s["breadcrumb"], s["location"], render_blocks(s["blocks"])) for s in sections]
    result = [part for sec in result for part in _split_large(sec, lambda a, b, loc=sec.location: loc)]
    return Book(
        title=clean_title(title, path), authors=clean_authors(authors), path=path, format="epub",
        sections=finalize_sections(result),
    )


# ---------------------------------------------------------------------------- PDF

_MONO_FONT = re.compile(r"mono|courier|consol|menlo|code|inconsolata|typewriter|mn(-|$)|(^|[-+])tt", re.I)
_PAGE_NUMBER = re.compile(r"^\s*(page\s*)?\d{1,4}\s*$", re.I)


def _is_mono(span: dict) -> bool:
    return bool(span.get("flags", 0) & 8) or bool(_MONO_FONT.search(span.get("font", "")))


def _join_lines(lines: list[str]) -> str:
    text = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
    return _WS.sub(" ", text)


@dataclass
class _Raw:
    mono: bool
    lines: list[str]
    size: float
    top: float
    bottom: float


def _raw_blocks(page) -> list[_Raw]:
    out: list[_Raw] = []
    for raw in page.get_text("dict", sort=True).get("blocks", []):
        if raw.get("type") != 0:
            continue
        lines, mono_chars, total_chars, size_sum = [], 0, 0, 0.0
        for line in raw.get("lines", []):
            spans = line.get("spans", [])
            lines.append("".join(s.get("text", "") for s in spans))
            for s in spans:
                n = len(s.get("text", "").strip())
                total_chars += n
                mono_chars += n if _is_mono(s) else 0
                size_sum += n * s.get("size", 0)
        if total_chars:
            x0, y0, x1, y1 = raw["bbox"]
            out.append(_Raw(mono_chars / total_chars > 0.6, lines, size_sum / total_chars, y0, y1))
    return out


def _body_size(pages: list[list[_Raw]]) -> float:
    sizes: Counter[float] = Counter()
    for raws in pages:
        for r in raws:
            if not r.mono:
                sizes[round(r.size, 1)] += sum(len(l) for l in r.lines)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _heading_levels(pages: list[list[_Raw]], body: float) -> list[float]:
    """Tailles de police des titres, de la plus grande à la plus petite (une par niveau)."""
    sizes: Counter[float] = Counter()
    for raws in pages:
        for r in raws:
            if not r.mono and r.size >= body * 1.15:
                sizes[round(r.size * 2) / 2] += 1
    return sorted((s for s, n in sizes.items() if n >= 2), reverse=True)


_CAPTION = re.compile(r"^(figure|fig\.|table|listing|exhibit|example)\s*[\dA-Z]+(\.\d+)*[.:]?\s", re.I)


def _page_blocks(raws: list[_Raw], body: float, levels: list[float]) -> list[Block]:
    """Assemble les blocs PDF : lignes d'un même paragraphe, code, titres (police plus grande)."""
    blocks: list[Block] = []
    prev: _Raw | None = None
    for r in raws:
        if r.mono:
            code = "\n".join(l.rstrip() for l in r.lines)
            if blocks and blocks[-1].kind == "code" and prev and prev.mono:
                blocks[-1].text += "\n" + code
            else:
                blocks.append(Block("code", code))
        else:
            text = _join_lines(r.lines)
            is_heading = (
                r.size >= body * 1.15 and len(text) < 150
                and re.search(r"[^\W\d_]", text) and not _CAPTION.match(text)
            )
            if is_heading:
                rank = sum(1 for size in levels if size > r.size + 0.25)
                blocks.append(Block("heading", text, min(2 + rank, 6)))
            elif (
                prev is not None and not prev.mono and blocks and blocks[-1].kind == "para"
                and abs(prev.size - r.size) < 0.5 and 0 <= r.top - prev.bottom < 0.5 * r.size
            ):
                blocks[-1].text = _join_lines([blocks[-1].text, text])
            else:
                blocks.append(Block("para", text))
        prev = r
    return blocks


def _running_texts(pages: list[list[Block]]) -> set[str]:
    """Textes courts répétés sur beaucoup de pages (en-têtes et pieds de page)."""
    if len(pages) < 3:
        return set()
    counts: Counter[str] = Counter()
    for blocks in pages:
        seen = {_running_key(b.text) for b in blocks if b.kind in ("para", "heading") and len(b.text) < 80}
        counts.update(seen)
    return {t for t, n in counts.items() if n >= max(3, 0.3 * len(pages))}


def _running_key(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower())


def extract_pdf(path: Path, toc_depth: int | None = None) -> Book:
    import pymupdf

    doc = pymupdf.open(str(path))
    meta = doc.metadata or {}

    raw_pages = [_raw_blocks(p) for p in doc]
    body = _body_size(raw_pages)
    levels = _heading_levels(raw_pages, body)
    pages = [_page_blocks(raws, body, levels) for raws in raw_pages]
    running = _running_texts(pages)

    full_parts: list[str] = []
    page_starts: list[int] = []
    offset = 0
    for blocks in pages:
        kept = [
            b for b in blocks
            if not (b.kind in ("para", "heading") and (_PAGE_NUMBER.match(b.text) or _running_key(b.text) in running))
        ]
        text = render_blocks(kept)
        page_starts.append(offset)
        full_parts.append(text)
        offset += len(text) + 2
    full = "\n\n".join(full_parts)

    def page_of(pos: int) -> int:
        return max(bisect.bisect_right(page_starts, pos) - 1, 0) + 1

    def loc(start: int, end: int) -> str:
        a, b = page_of(start), page_of(max(end - 1, start))
        return f"p. {a}" if a == b else f"p. {a}-{b}"

    toc = [(lvl, t.strip(), pg) for lvl, t, pg, *_ in doc.get_toc(simple=True) if pg >= 1 and t.strip()]
    sections: list[Section] = []

    if toc:
        # Position de chaque entrée du sommaire dans le texte, et fil d'Ariane.
        marks: list[tuple[int, int, str, list[str]]] = []
        stack: list[tuple[int, str]] = []
        last_pos = 0
        for lvl, entry_title, pg in toc:
            pg = min(pg, len(page_starts))
            region_start = page_starts[pg - 1]
            region_end = page_starts[pg] if pg < len(page_starts) else len(full)
            words = entry_title.split()[:8]
            pattern = r"\W*".join(re.escape(w) for w in words)
            m = None
            if words:  # de préférence en début de ligne, sinon n'importe où sur la page
                m = re.compile(r"^\W*" + pattern, re.I | re.M).search(full, region_start, region_end)
                m = m or re.compile(pattern, re.I).search(full, region_start, region_end)
            pos = max(m.start() if m else region_start, last_pos)
            last_pos = pos
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            marks.append((pos, lvl, entry_title, [t for _, t in stack]))
            stack.append((lvl, entry_title))

        def build(depth: int) -> list[tuple[Section, int]]:
            chosen = [mk for mk in marks if mk[1] <= depth]
            out = []
            for i, (pos, _lvl, entry_title, crumbs) in enumerate(chosen):
                end = chosen[i + 1][0] if i + 1 < len(chosen) else len(full)
                out.append((Section(entry_title, crumbs, loc(pos, end), full[pos:end].strip()), pos))
            return out

        # Profondeur de sommaire la plus faible où 90 % des sections restent de taille raisonnable.
        max_depth = max(lvl for _, lvl, _, _ in marks)
        depths = [toc_depth] if toc_depth else range(1, min(max_depth, 3) + 1)
        for depth in depths:
            built = build(depth)
            sizes = sorted(
                _word_count(sec.text) for sec, _ in built
                if not _is_skipped(sec) and _word_count(sec.text) >= MIN_SECTION_WORDS
            )
            if sizes and sizes[int(len(sizes) * 0.9)] <= LARGE_SECTION_WORDS:
                break
        for sec, pos in built:
            base = pos + (len(full[pos:]) - len(full[pos:].lstrip()))
            sections.extend(_split_large(sec, lambda a, b, base=base: loc(base + a, base + b)))
    else:
        for first in range(0, len(page_starts), PDF_FALLBACK_PAGES):
            last = min(first + PDF_FALLBACK_PAGES, len(page_starts))
            text = "\n\n".join(full_parts[first:last]).strip()
            label = f"Pages {first + 1}-{last}"
            sections.append(Section(label, [], f"p. {first + 1}-{last}", text))

    doc.close()
    return Book(
        title=clean_title(meta.get("title"), path), authors=clean_authors(meta.get("author")), path=path,
        format="pdf", sections=finalize_sections(sections),
    )
