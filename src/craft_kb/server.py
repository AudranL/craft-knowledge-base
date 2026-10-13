"""Serveur MCP (stdio) exposant la base de connaissance craftsmanship."""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .config import Settings
from .embed import OllamaEmbedder
from .kb import KnowledgeBase, SearchResult

INSTRUCTIONS = """\
Bibliothèque locale d'ebooks sur le software craftsmanship (TDD, refactoring, DDD, clean code,
architecture, XP…). Pour toute question sur ces notions, cherche d'abord dans la bibliothèque :
1. `search_passages` avec des mots-clés précis ; essaie aussi les termes anglais des livres
   (ex. « agrégat » → « aggregate ») et plusieurs angles si besoin.
2. `read_section` sur les section_id les plus pertinents pour lire le contexte complet.
Cite toujours tes sources (livre, section, pages) et distingue ce qui vient des livres de tes
propres connaissances."""


def format_search(query: str, result: SearchResult) -> str:
    lines = [f"Recherche « {query} » — mode {result.mode}, {len(result.hits)} passage(s)."]
    if result.warning:
        lines.append(f"⚠ {result.warning}")
    if not result.hits:
        lines.append("Aucun passage trouvé. Essaie d'autres mots-clés, en anglais, ou un synonyme.")
    for i, hit in enumerate(result.hits, 1):
        by = f" — {hit.authors}" if hit.authors else ""
        where = f" · {hit.location}" if hit.location else ""
        sub = f" › {hit.heading}" if hit.heading and not hit.section.endswith(hit.heading) else ""
        lines.append(
            f"\n[{i}] {hit.book}{by}\n"
            f"section_id={hit.section_id} · {hit.section}{sub}{where}\n\n{hit.text}"
        )
    return "\n".join(lines)


def format_section(data: dict) -> str:
    by = f" — {data['authors']}" if data["authors"] else ""
    header = [
        f"{data['book']}{by}",
        f"section_id={data['section_id']} · {data['section']}" + (f" · {data['location']}" if data["location"] else ""),
    ]
    end = data["next_offset"] or data["total_chars"]
    header.append(f"Caractères {data['offset']}–{end} sur {data['total_chars']}.")
    footer = []
    if data["next_offset"] is not None:
        footer.append(f"Suite de la section : read_section(section_id={data['section_id']}, offset={data['next_offset']})")
    if data["previous_section_id"]:
        footer.append(f"Section précédente : section_id={data['previous_section_id']}")
    if data["next_section_id"]:
        footer.append(f"Section suivante : section_id={data['next_section_id']}")
    return "\n".join(header) + "\n\n" + data["text"] + ("\n\n---\n" + "\n".join(footer) if footer else "")


_SOURCES = (
    "Appuie-toi sur la bibliothèque : lance `search_passages` sous plusieurs angles (termes "
    "français et anglais, synonymes, notions voisines), puis `read_section` sur les 2 à 4 "
    "sections les plus pertinentes. Si plusieurs auteurs traitent la notion, compare-les."
)
_CITE = (
    "Cite chaque affirmation tirée des livres sous la forme (Livre, section, pages). "
    "Signale clairement ce qui vient de tes connaissances générales plutôt que de la bibliothèque, "
    "et dis-le si la bibliothèque ne couvre pas la notion."
)


def create_server(kb: KnowledgeBase) -> MCPServer:
    server = MCPServer(name="craft-kb", instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Recherche hybride (plein texte + sémantique) dans les ebooks de la bibliothèque. "
            "Renvoie des passages avec leur livre, section_id, section et pages."
        )
    )
    def search_passages(
        query: Annotated[str, Field(description="Notion, question ou mots-clés (les livres sont souvent en anglais)")],
        limit: Annotated[int, Field(ge=1, le=20, description="Nombre de passages à renvoyer")] = 6,
        book: Annotated[
            str | None, Field(description="Filtre optionnel sur le titre, l'auteur ou le nom de fichier")
        ] = None,
    ) -> str:
        return format_search(query, kb.search(query, limit=limit, book=book))

    @server.tool(
        description=(
            "Lit une section (chapitre ou sous-chapitre) complète à partir de son section_id. "
            "Les longues sections sont paginées via offset."
        )
    )
    def read_section(
        section_id: Annotated[int, Field(description="section_id renvoyé par search_passages")],
        offset: Annotated[int, Field(ge=0, description="Position de départ, pour lire la suite")] = 0,
        max_chars: Annotated[int, Field(ge=1000, le=40000, description="Taille maximale du texte renvoyé")] = 12000,
    ) -> str:
        data = kb.read_section(section_id, offset=offset, max_chars=max_chars)
        if data is None:
            return f"Aucune section avec section_id={section_id}."
        return format_section(data)

    @server.prompt(
        name="fiche_veille",
        title="Fiche de veille",
        description="Construit une fiche de veille sur une notion à partir de la bibliothèque.",
    )
    def fiche_veille(notion: str, langage: str = "Java") -> str:
        return f"""Construis une fiche de veille sur la notion suivante : **{notion}**.

{_SOURCES}

Structure de la fiche (Markdown) :
1. **En une phrase** — la définition la plus juste possible.
2. **Le problème résolu** — pourquoi la notion existe, dans quel contexte elle apparaît.
3. **Principes clés** — 3 à 7 points, chacun sourcé.
4. **Exemple concret** — un exemple court en {langage}, avant/après si c'est pertinent.
5. **Pièges et mauvais usages** — anti-patterns, contresens fréquents, limites.
6. **Points de vue des auteurs** — convergences et divergences entre les livres.
7. **Notions liées** — avec ce qu'elles apportent à celle-ci.
8. **Pour aller plus loin** — les sections à lire en entier (livre, section, pages).
9. **Sources**.

{_CITE}"""

    @server.prompt(
        name="creer_skill",
        title="Créer une skill",
        description="Transforme une notion de la bibliothèque en skill Claude (SKILL.md).",
    )
    def creer_skill(notion: str, objectif: str = "") -> str:
        goal = f"\nObjectif de la skill : {objectif}\n" if objectif else ""
        return f"""Crée une skill Claude (fichier SKILL.md) qui permet d'appliquer **{notion}** dans du vrai code.
{goal}
{_SOURCES}

Format attendu :
```
---
name: <nom-en-kebab-case>
description: <une phrase qui dit QUAND utiliser la skill : déclencheurs concrets, situations, mots-clés>
---
```
Puis le corps de la skill :
- **Quand l'appliquer / quand ne pas l'appliquer**.
- **Procédure** — étapes numérotées, actionnables par un agent qui code.
- **Checklist de vérification** — critères observables dans le code ou les tests.
- **Exemples** — un cas « à faire » et un cas « à éviter », courts.
- **Anti-patterns** à détecter.
- **Références** — livres, sections et pages d'où viennent les règles.

Écris des instructions impératives et vérifiables, sans paraphraser les livres longuement.
Pas de règle qui ne soit justifiée par une source ou par une raison explicite. {_CITE}"""

    @server.prompt(
        name="instructions_agent",
        title="Instructions d'agent",
        description="Rédige des règles pour CLAUDE.md / AGENTS.md à partir d'une notion de la bibliothèque.",
    )
    def instructions_agent(notion: str, contexte: str = "") -> str:
        ctx = f"\nContexte du projet : {contexte}\n" if contexte else ""
        return f"""Rédige une section d'instructions pour un agent de code (CLAUDE.md ou AGENTS.md) sur **{notion}**.
{ctx}
{_SOURCES}

Contraintes :
- 5 à 12 règles, impératives, chacune vérifiable en relisant un diff.
- Après chaque règle, une justification d'une ligne avec sa source (Livre, section).
- Un mini-exemple « à faire / à éviter » uniquement pour les règles ambiguës.
- Rien de générique ou d'évident pour un agent ; privilégie ce qui change réellement sa façon de coder.
- Termine par les signaux qui doivent pousser l'agent à s'arrêter et demander.

Livre le résultat dans un bloc Markdown prêt à coller, puis la liste des sources. {_CITE}"""

    return server


def main() -> None:
    settings = Settings.from_env()
    kb = KnowledgeBase(settings.db_path, OllamaEmbedder(settings.ollama_url, settings.embed_model))
    create_server(kb).run("stdio")
