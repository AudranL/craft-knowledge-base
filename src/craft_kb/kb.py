"""Base de connaissance : stockage SQLite, indexation et recherche hybride."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .chunking import chunk_section
from .embed import Embedder, EmbeddingUnavailable
from .extract import extract_book

SUPPORTED = {".epub", ".pdf"}
RRF_K = 60
CANDIDATES = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS books(
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT,
    file_path TEXT UNIQUE NOT NULL,
    file_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections(
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    breadcrumb TEXT NOT NULL,
    location TEXT,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    heading TEXT,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_sections_book ON sections(book_id, position);
CREATE INDEX IF NOT EXISTS idx_chunks_section ON chunks(section_id, position);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading, section_title, book_title,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""

STOPWORDS = set(
    """a an and are as at be but by can do does for from how i if in into is it its of on or so
    than that the their then there these this to was what when where which who why will with
    you your au aux avec ce ces comme comment dans de des du elle en est et il ils je la le les
    leur mais ne ou où par pas plus pour quand que qui quoi sa se ses son sur ta te tes ton un
    une vos votre être faut qu est quoi quel quelle quels quelles""".split()
)


@dataclass
class Hit:
    chunk_id: int
    section_id: int
    book: str
    authors: str
    section: str
    heading: str
    location: str
    text: str
    score: float


@dataclass
class SearchResult:
    mode: str
    hits: list[Hit] = field(default_factory=list)
    warning: str | None = None


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fts_query(query: str) -> str | None:
    tokens = re.findall(r"\w+", query.lower())
    useful = [t for t in tokens if t not in STOPWORDS and len(t) > 1] or tokens
    if not useful:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(useful))


class KnowledgeBase:
    def __init__(self, db_path: Path, embedder: Embedder | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._vec_lock = threading.Lock()
        self._vec_cache: tuple[tuple, np.ndarray, np.ndarray, np.ndarray] | None = None
        with self._conn() as con:
            con.executescript(SCHEMA)

    # ------------------------------------------------------------------ infra
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _meta(self, con: sqlite3.Connection, key: str) -> str | None:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    # -------------------------------------------------------------- ingestion
    def ingest_file(self, path: Path) -> str:
        path = path.resolve()
        digest = _file_hash(path)
        with self._conn() as con:
            row = con.execute("SELECT id, file_hash FROM books WHERE file_path = ?", (str(path),)).fetchone()
            if row and row["file_hash"] == digest:
                return "inchangé"
        book = extract_book(path)
        if not book.sections:
            return "vide (aucun texte extrait : PDF scanné ou protégé par DRM ?)"
        with self._conn() as con:
            if row:
                self._delete_book(con, row["id"])
            cur = con.execute(
                "INSERT INTO books(title, authors, file_path, file_hash, format, added_at) VALUES (?,?,?,?,?,?)",
                (book.title, book.authors, str(path), digest, book.format, datetime.now(timezone.utc).isoformat()),
            )
            book_id = cur.lastrowid
            for pos, sec in enumerate(book.sections):
                breadcrumb = " > ".join([*sec.breadcrumb, sec.title])
                sec_id = con.execute(
                    "INSERT INTO sections(book_id, position, title, breadcrumb, location, text) VALUES (?,?,?,?,?,?)",
                    (book_id, pos, sec.title, breadcrumb, sec.location, sec.text),
                ).lastrowid
                for cpos, chunk in enumerate(chunk_section(sec.text, sec.title)):
                    chunk_id = con.execute(
                        "INSERT INTO chunks(section_id, book_id, position, heading, text) VALUES (?,?,?,?,?)",
                        (sec_id, book_id, cpos, chunk.heading, chunk.text),
                    ).lastrowid
                    con.execute(
                        "INSERT INTO chunks_fts(rowid, text, heading, section_title, book_title) VALUES (?,?,?,?,?)",
                        (chunk_id, chunk.text, chunk.heading, breadcrumb, book.title),
                    )
        return "mis à jour" if row else "ajouté"

    def _delete_book(self, con: sqlite3.Connection, book_id: int) -> None:
        con.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE book_id = ?)", (book_id,))
        con.execute("DELETE FROM books WHERE id = ?", (book_id,))

    def ingest_dir(
        self, root: Path, prune: bool = False, log: Callable[[str], None] = print
    ) -> dict[str, int]:
        root = root.expanduser().resolve()
        files = sorted(p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED and p.is_file())
        counts: dict[str, int] = {}
        for path in files:
            try:
                status = self.ingest_file(path)
            except Exception as exc:  # un livre illisible ne bloque pas les autres
                status = "erreur"
                log(f"  ✗ {path.name} : {exc}")
            else:
                log(f"  {'·' if status == 'inchangé' else '✓'} {path.name} : {status}")
            key = status.split(" ")[0]
            counts[key] = counts.get(key, 0) + 1
        if prune:
            present = {str(p.resolve()) for p in files}
            with self._conn() as con:
                for row in con.execute("SELECT id, file_path, title FROM books").fetchall():
                    if row["file_path"].startswith(str(root)) and row["file_path"] not in present:
                        self._delete_book(con, row["id"])
                        log(f"  − {row['title']} : retiré (fichier supprimé)")
                        counts["retiré"] = counts.get("retiré", 0) + 1
        return counts

    def remove_book(self, book_id: int) -> bool:
        with self._conn() as con:
            exists = con.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone()
            if exists:
                self._delete_book(con, book_id)
            return bool(exists)

    # ------------------------------------------------------------- embeddings
    def embed_missing(self, batch: int = 64, log: Callable[[str], None] = print) -> int:
        if self.embedder is None:
            return 0
        model = self.embedder.model
        with self._conn() as con:
            stored = self._meta(con, "embed_model")
            if stored and stored != model:
                log(f"  Modèle d'embedding changé ({stored} → {model}) : ré-indexation sémantique complète.")
                con.execute("UPDATE chunks SET embedding = NULL")
            total = con.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0]
        done = 0
        while True:
            with self._conn() as con:
                rows = con.execute(
                    """SELECT c.id, c.text, c.heading, s.breadcrumb, b.title AS book
                       FROM chunks c JOIN sections s ON s.id = c.section_id JOIN books b ON b.id = c.book_id
                       WHERE c.embedding IS NULL LIMIT ?""",
                    (batch,),
                ).fetchall()
            if not rows:
                break
            texts = [self._embedding_text(r["book"], r["breadcrumb"], r["heading"], r["text"]) for r in rows]
            vectors = self.embedder.embed(texts)
            with self._conn() as con:
                con.executemany(
                    "UPDATE chunks SET embedding = ? WHERE id = ?",
                    [(v.astype(np.float32).tobytes(), r["id"]) for v, r in zip(vectors, rows)],
                )
                con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('embed_model', ?)", (model,))
            done += len(rows)
            log(f"  embeddings : {done}/{total}")
        return done

    @staticmethod
    def _embedding_text(book: str, breadcrumb: str, heading: str | None, text: str) -> str:
        context = f"{book} — {breadcrumb}"
        if heading and not breadcrumb.endswith(heading):
            context += f" — {heading}"
        return f"{context}\n\n{text}"

    def _vectors(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        with self._conn() as con:
            key = tuple(
                con.execute(
                    "SELECT count(*), max(id), (SELECT value FROM meta WHERE key='embed_model') "
                    "FROM chunks WHERE embedding IS NOT NULL"
                ).fetchone()
            )
            if key[0] == 0:
                return None
            with self._vec_lock:
                if self._vec_cache and self._vec_cache[0] == key:
                    return self._vec_cache[1:]
            rows = con.execute("SELECT id, book_id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall()
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        books = np.array([r["book_id"] for r in rows], dtype=np.int64)
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        with self._vec_lock:
            self._vec_cache = (key, ids, books, matrix)
        return ids, books, matrix

    # -------------------------------------------------------------- recherche
    def _book_filter(self, con: sqlite3.Connection, book: str | None) -> list[int] | None:
        if not book:
            return None
        like = f"%{book.lower()}%"
        rows = con.execute(
            "SELECT id FROM books WHERE lower(title) LIKE ? OR lower(authors) LIKE ? OR lower(file_path) LIKE ?",
            (like, like, like),
        ).fetchall()
        return [r["id"] for r in rows]

    def search(self, query: str, limit: int = 6, book: str | None = None) -> SearchResult:
        warning = None
        with self._conn() as con:
            book_ids = self._book_filter(con, book)
            if book_ids == []:
                return SearchResult("aucun", warning=f"Aucun livre ne correspond au filtre « {book} ».")
            fts_ranked: list[int] = []
            expr = fts_query(query)
            if expr:
                sql = "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
                params: list = [expr]
                if book_ids:
                    sql += f" AND rowid IN (SELECT id FROM chunks WHERE book_id IN ({','.join('?' * len(book_ids))}))"
                    params += book_ids
                sql += " ORDER BY bm25(chunks_fts, 1.0, 1.5, 1.5, 0.3) LIMIT ?"
                fts_ranked = [r[0] for r in con.execute(sql, [*params, CANDIDATES]).fetchall()]
            stored_model = self._meta(con, "embed_model")

        vec_ranked: list[int] = []
        if self.embedder is not None:
            if stored_model and stored_model != self.embedder.model:
                warning = (
                    f"Index sémantique construit avec {stored_model}, serveur configuré sur "
                    f"{self.embedder.model} : relance `craft-kb embed`."
                )
            else:
                vec_ranked, warning = self._semantic(query, book_ids)
        else:
            warning = "Recherche sémantique désactivée."

        scores: dict[int, float] = {}
        for ranked in (fts_ranked, vec_ranked):
            for rank, chunk_id in enumerate(ranked):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        top = sorted(scores, key=scores.get, reverse=True)[:limit]
        mode = "hybride" if fts_ranked and vec_ranked else ("sémantique" if vec_ranked else "plein texte")
        return SearchResult(mode, self._hits(top, scores), warning)

    def _semantic(self, query: str, book_ids: list[int] | None) -> tuple[list[int], str | None]:
        data = self._vectors()
        if data is None:
            return [], "Aucun embedding en base : lance `craft-kb embed` (Ollama requis)."
        ids, books, matrix = data
        try:
            q = self.embedder.embed([query])[0]
        except EmbeddingUnavailable as exc:
            return [], f"Recherche sémantique indisponible ({exc}) ; résultats en plein texte uniquement."
        sims = matrix @ q
        if book_ids:
            sims = np.where(np.isin(books, book_ids), sims, -np.inf)
        k = min(CANDIDATES, len(sims))
        best = np.argpartition(-sims, k - 1)[:k]
        best = best[np.argsort(-sims[best])]
        return [int(ids[i]) for i in best if np.isfinite(sims[i])], None

    def _hits(self, chunk_ids: list[int], scores: dict[int, float]) -> list[Hit]:
        if not chunk_ids:
            return []
        with self._conn() as con:
            rows = con.execute(
                f"""SELECT c.id, c.section_id, c.heading, c.text, s.breadcrumb, s.location, b.title, b.authors
                    FROM chunks c JOIN sections s ON s.id = c.section_id JOIN books b ON b.id = c.book_id
                    WHERE c.id IN ({','.join('?' * len(chunk_ids))})""",
                chunk_ids,
            ).fetchall()
        by_id = {r["id"]: r for r in rows}
        return [
            Hit(
                chunk_id=cid,
                section_id=by_id[cid]["section_id"],
                book=by_id[cid]["title"],
                authors=by_id[cid]["authors"] or "",
                section=by_id[cid]["breadcrumb"],
                heading=by_id[cid]["heading"] or "",
                location=by_id[cid]["location"] or "",
                text=by_id[cid]["text"],
                score=round(scores[cid], 4),
            )
            for cid in chunk_ids
            if cid in by_id
        ]

    # ---------------------------------------------------------------- lecture
    def read_section(self, section_id: int, offset: int = 0, max_chars: int = 12000) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                """SELECT s.*, b.title AS book, b.authors FROM sections s JOIN books b ON b.id = s.book_id
                   WHERE s.id = ?""",
                (section_id,),
            ).fetchone()
            if row is None:
                return None
            neighbours = con.execute(
                """SELECT
                     (SELECT id FROM sections WHERE book_id = ? AND position < ? ORDER BY position DESC LIMIT 1),
                     (SELECT id FROM sections WHERE book_id = ? AND position > ? ORDER BY position LIMIT 1)""",
                (row["book_id"], row["position"], row["book_id"], row["position"]),
            ).fetchone()
        text = row["text"]
        offset = max(0, min(offset, len(text)))
        end = min(offset + max_chars, len(text))
        if end < len(text):
            floor = offset + int(max_chars * 0.6)
            cut = text.rfind("\n\n", floor, end)
            if cut <= offset:  # pas de fin de paragraphe : on coupe au moins entre deux mots
                cut = text.rfind(" ", floor, end)
            end = cut if cut > offset else end
        return {
            "section_id": row["id"],
            "book": row["book"],
            "authors": row["authors"] or "",
            "section": row["breadcrumb"],
            "location": row["location"] or "",
            "offset": offset,
            "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "previous_section_id": neighbours[0],
            "next_section_id": neighbours[1],
            "text": text[offset:end].strip(),
        }

    # ------------------------------------------------------------------ état
    def books(self) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                """SELECT b.id, b.title, b.authors, b.format, b.file_path,
                          (SELECT count(*) FROM sections WHERE book_id = b.id) AS sections,
                          (SELECT count(*) FROM chunks WHERE book_id = b.id) AS chunks,
                          (SELECT count(*) FROM chunks WHERE book_id = b.id AND embedding IS NOT NULL) AS embedded
                   FROM books b ORDER BY b.title"""
            ).fetchall()
        return [dict(r) for r in rows]

    def embedding_model(self) -> str | None:
        with self._conn() as con:
            return self._meta(con, "embed_model")
