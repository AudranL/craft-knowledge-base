"""Ligne de commande : craft-kb ingest | embed | status | search | remove | serve."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .embed import EmbeddingUnavailable, OllamaEmbedder
from .kb import KnowledgeBase


def _kb(settings: Settings, semantic: bool = True) -> KnowledgeBase:
    embedder = OllamaEmbedder(settings.ollama_url, settings.embed_model) if semantic else None
    return KnowledgeBase(settings.db_path, embedder)


def _embed(kb: KnowledgeBase) -> None:
    try:
        n = kb.embed_missing()
        print(f"Embeddings calculés : {n}")
    except EmbeddingUnavailable as exc:
        print(f"⚠ {exc}\n  L'index plein texte est prêt ; relance `craft-kb embed` quand Ollama tourne.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="craft-kb", description="Base de connaissance locale d'ebooks.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="Indexer un dossier d'ebooks (EPUB, PDF), de façon incrémentale")
    p_ingest.add_argument("folder", type=Path)
    p_ingest.add_argument("--prune", action="store_true", help="Retirer les livres dont le fichier a disparu")
    p_ingest.add_argument("--no-embed", action="store_true", help="Plein texte uniquement, sans Ollama")

    sub.add_parser("embed", help="Calculer les embeddings manquants (Ollama)")
    sub.add_parser("status", help="Lister les livres indexés")

    p_search = sub.add_parser("search", help="Tester une recherche")
    p_search.add_argument("query")
    p_search.add_argument("-k", "--limit", type=int, default=5)
    p_search.add_argument("--book")

    p_remove = sub.add_parser("remove", help="Retirer un livre de la base (id donné par status)")
    p_remove.add_argument("book_id", type=int)

    sub.add_parser("serve", help="Lancer le serveur MCP (stdio)")

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "serve":
        from .server import main as serve

        serve()
        return

    if args.cmd == "ingest":
        if not args.folder.expanduser().is_dir():
            sys.exit(f"Dossier introuvable : {args.folder}")
        kb = _kb(settings, semantic=not args.no_embed)
        print(f"Indexation de {args.folder} → {settings.db_path}")
        counts = kb.ingest_dir(args.folder, prune=args.prune)
        print("Bilan : " + ", ".join(f"{k} {v}" for k, v in counts.items()) if counts else "Aucun EPUB/PDF trouvé.")
        if not args.no_embed:
            _embed(kb)
    elif args.cmd == "embed":
        _embed(_kb(settings))
    elif args.cmd == "status":
        kb = _kb(settings, semantic=False)
        books = kb.books()
        print(f"Base : {settings.db_path} · modèle d'embedding : {kb.embedding_model() or 'aucun'}")
        for b in books:
            print(
                f"  [{b['id']}] {b['title']} — {b['authors'] or '?'} ({b['format']}) · "
                f"{b['sections']} sections · {b['embedded']}/{b['chunks']} passages vectorisés"
            )
        if not books:
            print("  (vide) — lance `craft-kb ingest <dossier>`")
    elif args.cmd == "search":
        from .server import format_search

        print(format_search(args.query, _kb(settings).search(args.query, limit=args.limit, book=args.book)))
    elif args.cmd == "remove":
        ok = _kb(settings, semantic=False).remove_book(args.book_id)
        print("Livre retiré." if ok else "Aucun livre avec cet id.")


if __name__ == "__main__":
    main()
