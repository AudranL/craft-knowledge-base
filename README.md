# craft-kb

Base de connaissance locale construite à partir de tes ebooks (EPUB et PDF) sur le software
craftsmanship, mise à disposition de Claude par un serveur MCP local. Tout reste sur ta machine :
le texte, l'index et les embeddings.

```
ebooks/ ──ingest──▶ SQLite (sections, passages, index FTS5, vecteurs) ◀──MCP stdio── Claude
                         ▲
                 Ollama (bge-m3)
```

## Installation (macOS)

```bash
# 1. Outils
brew install uv ollama
ollama serve            # ou lance l'application Ollama
ollama pull bge-m3      # modèle d'embedding multilingue (~1,2 Go)

# 2. Projet
cd ~/Dev/craft-kb       # là où tu as décompressé le projet
uv sync

# 3. Indexation (incrémentale : relance-la à chaque nouvel ebook)
uv run craft-kb ingest ~/Ebooks/craftsmanship
uv run craft-kb status
uv run craft-kb search "aggregate invariants"
```

`bge-m3` est multilingue : une question en français retrouve des passages de livres en anglais.
Pour changer de modèle : `CRAFT_KB_EMBED_MODEL=nomic-embed-text uv run craft-kb embed`
(l'index sémantique est alors recalculé).

## Brancher le MCP

**Claude Code**

```bash
claude mcp add --scope user craft-kb -- uv --directory ~/Dev/craft-kb run craft-kb serve
```

**Claude Desktop** : dans `~/Library/Application Support/Claude/claude_desktop_config.json`.
Les chemins doivent être absolus, l'application ne connaît pas ton `PATH` (`which uv` donne celui de uv).

```json
{
  "mcpServers": {
    "craft-kb": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/Users/<toi>/Dev/craft-kb", "run", "craft-kb", "serve"]
    }
  }
}
```

Redémarre Claude Desktop après modification.

## Ce que Claude obtient

| | |
|---|---|
| `search_passages(query, limit, book)` | Recherche hybride : plein texte (FTS5, BM25) et sémantique (cosinus), fusionnés par Reciprocal Rank Fusion. Renvoie des passages avec livre, `section_id`, chemin de section et pages. `book` filtre sur le titre, l'auteur ou le nom de fichier. |
| `read_section(section_id, offset, max_chars)` | Lit un chapitre ou une section en entier, paginé, avec les sections précédente et suivante. |
| Prompt `fiche_veille(notion, langage)` | Fiche de veille sourcée : définition, problème résolu, principes, exemple, pièges, divergences entre auteurs. |
| Prompt `creer_skill(notion, objectif)` | Génère un `SKILL.md` : déclencheurs, procédure, checklist, exemples, références. |
| Prompt `instructions_agent(notion, contexte)` | Règles vérifiables pour un `CLAUDE.md` / `AGENTS.md`, chacune justifiée par une source. |

Si Ollama n'est pas lancé, la recherche bascule en plein texte et le signale dans le résultat.

## Commandes

| Commande | Rôle |
|---|---|
| `craft-kb ingest <dossier> [--prune] [--no-embed]` | Indexe les nouveaux livres ou ceux qui ont changé (hash du fichier). `--prune` retire ceux qui ont disparu du dossier. |
| `craft-kb embed` | Calcule les embeddings manquants (après une ingestion sans Ollama, par exemple). |
| `craft-kb status` | Liste les livres, leurs sections et la couverture vectorielle. |
| `craft-kb search "…" [--book …]` | Teste une recherche depuis le terminal. |
| `craft-kb remove <id>` | Retire un livre de la base. |
| `craft-kb serve` | Serveur MCP (stdio), lancé par Claude. |

Configuration par variables d'environnement : `CRAFT_KB_DB` (défaut `~/.craft-kb/kb.sqlite`),
`OLLAMA_URL` (défaut `http://localhost:11434`), `CRAFT_KB_EMBED_MODEL` (défaut `bge-m3`).

## Comment les livres sont découpés

- **EPUB** : sections calquées sur la table des matières, y compris les sous-sections pointant
  vers une ancre au sein d'un même fichier. Titres, paragraphes et blocs de code (```) sont conservés.
- **PDF** : sections tirées du sommaire du PDF (deux niveaux), avec les pages. Les lignes sont
  regroupées en paragraphes, les titres sont repérés à leur taille de police, le code à sa police
  à chasse fixe, et les en-têtes ou pieds de page répétés sont retirés. Sans sommaire, le livre
  est découpé par tranches de 10 pages.
- Les passages indexés font environ 300 mots, se recouvrent légèrement et gardent leur sous-titre.
  Le contexte (livre › chapitre › section) est ajouté au texte vectorisé.
- Les sections « Index » sont ignorées, car elles polluent la recherche plein texte.

Limites : les fichiers protégés par DRM et les PDF scannés (sans couche texte) ne donnent aucun
texte ; l'ingestion les signale comme « vide ».

## Développement

```bash
uv run pytest
```

Les tests génèrent un EPUB et un PDF d'exemple et remplacent Ollama par un embedder factice.
