"""Découpage des sections en passages (chunks) pour l'indexation."""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_WORDS = 280
MAX_WORDS = 450
OVERLAP_MAX_WORDS = 80

_FENCE = re.compile(r"(```.*?```)", re.S)
_HEADING = re.compile(r"^#{2,6} [^\n]{1,150}$")


@dataclass
class Chunk:
    text: str
    heading: str


def _units(text: str) -> list[str]:
    units: list[str] = []
    for part in _FENCE.split(text):
        if part.startswith("```"):
            units.append(part)
        else:
            units.extend(p.strip() for p in part.split("\n\n") if p.strip())
    return units


def _split_long(unit: str) -> list[str]:
    words = unit.split(" ") if not unit.startswith("```") else unit.split("\n")
    sep = " " if not unit.startswith("```") else "\n"
    size = MAX_WORDS if sep == " " else 60  # 60 lignes de code par morceau
    pieces = [sep.join(words[i : i + size]) for i in range(0, len(words), size)]
    if sep == "\n":  # refermer / rouvrir les fences
        pieces = [p if p.startswith("```") else "```\n" + p for p in pieces]
        pieces = [p if p.rstrip().endswith("```") else p + "\n```" for p in pieces]
    return pieces


def _wc(text: str) -> int:
    return len(text.split())


def chunk_section(text: str, section_title: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = section_title
    current: list[str] = []
    current_heading = heading
    words = 0

    def emit() -> None:
        nonlocal current, words
        body = "\n\n".join(current).strip()
        if body:
            chunks.append(Chunk(body, current_heading))
        last = current[-1] if current else ""
        # Recouvrement : on reprend le dernier paragraphe s'il est court.
        if last and not _HEADING.match(last) and _wc(last) <= OVERLAP_MAX_WORDS:
            current, words = [last], _wc(last)
        else:
            current, words = [], 0

    for unit in _units(text):
        if _HEADING.match(unit):
            if words >= TARGET_WORDS // 2:
                emit()
                current, words = [], 0  # pas de recouvrement au-delà d'un titre
            heading = unit.lstrip("#").strip()
            if not current:
                current_heading = heading
            current.append(unit)
            continue
        pieces = _split_long(unit) if _wc(unit) > MAX_WORDS or unit.count("\n") > 80 else [unit]
        for piece in pieces:
            if words and words + _wc(piece) > MAX_WORDS:
                emit()
                current_heading = heading
            if not current:
                current_heading = heading
            current.append(piece)
            words += _wc(piece)
            if words >= TARGET_WORDS:
                emit()
                current_heading = heading
    # Dernier chunk : on évite de ne ré-émettre que le recouvrement.
    if current and (not chunks or "\n\n".join(current) not in chunks[-1].text):
        chunks.append(Chunk("\n\n".join(current).strip(), current_heading))
    return chunks
