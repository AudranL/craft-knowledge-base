#!/usr/bin/env bash
# Installe craft-kb, indexe un dossier d'ebooks et branche le serveur MCP sur Claude.
#
#   ./scripts/setup.sh ~/Ebooks/craftsmanship    # première fois
#   ./scripts/setup.sh                           # ensuite : réindexe le même dossier
#
# Relançable sans risque : chaque étape déjà faite est sautée, et seuls les livres
# nouveaux ou modifiés sont indexés.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.craft-kb"
LAST_DIR_FILE="${STATE_DIR}/ebooks-dir"
MODEL="${CRAFT_KB_EMBED_MODEL:-bge-m3}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
DESKTOP_CONFIG="${HOME}/Library/Application Support/Claude/claude_desktop_config.json"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok() { printf '  ✓ %s\n' "$1"; }
warn() { printf '  ⚠ %s\n' "$1"; }
die() { printf '\n✗ %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------- dossier d'ebooks
EBOOKS_DIR="${1:-}"
if [[ -z "$EBOOKS_DIR" && -f "$LAST_DIR_FILE" ]]; then
  EBOOKS_DIR="$(cat "$LAST_DIR_FILE")"
fi
[[ -n "$EBOOKS_DIR" ]] || die "Indique le dossier de tes ebooks : ./scripts/setup.sh ~/Ebooks/craftsmanship"
[[ -d "$EBOOKS_DIR" ]] || die "Dossier introuvable : $EBOOKS_DIR"
EBOOKS_DIR="$(cd "$EBOOKS_DIR" && pwd)"
case "$EBOOKS_DIR/" in
  "$PROJECT_DIR"/*) die "Range tes ebooks hors du dépôt git ($PROJECT_DIR)." ;;
esac
mkdir -p "$STATE_DIR"
printf '%s\n' "$EBOOKS_DIR" > "$LAST_DIR_FILE"

# ------------------------------------------------------------------------ outils
step "Outils"
need_brew() { command -v brew >/dev/null || die "Homebrew est requis pour installer $1 : https://brew.sh"; }
for tool in uv ollama; do
  if command -v "$tool" >/dev/null; then
    ok "$tool présent"
  else
    need_brew "$tool"
    brew install "$tool"
    ok "$tool installé"
  fi
done
UV="$(command -v uv)"

# ------------------------------------------------------------------------ Ollama
step "Ollama et modèle d'embedding ($MODEL)"
ollama_up() { curl -fsS "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; }
if ! ollama_up; then
  if command -v brew >/dev/null && brew list ollama >/dev/null 2>&1; then
    brew services start ollama >/dev/null
  else
    (ollama serve >/dev/null 2>&1 &)  # Ollama installé hors Homebrew (application)
  fi
  for _ in $(seq 1 30); do ollama_up && break; sleep 1; done
fi
if ollama_up; then
  ok "Ollama répond sur $OLLAMA_URL"
  if ollama list | awk 'NR > 1 { print $1 }' | grep -Eq "^${MODEL}(:|$)"; then
    ok "$MODEL déjà téléchargé"
  else
    ollama pull "$MODEL"
    ok "$MODEL téléchargé"
  fi
else
  warn "Ollama ne répond pas : indexation en plein texte seulement."
  warn "Relance ce script une fois Ollama démarré pour ajouter la recherche sémantique."
fi

# ------------------------------------------------------------------------ projet
step "Dépendances du projet"
"$UV" sync --quiet --project "$PROJECT_DIR"
ok "environnement Python prêt"

# ---------------------------------------------------------------------- ebooks
step "Indexation de $EBOOKS_DIR"
if [[ ! -f "$EBOOKS_DIR/craft-kb.toml" ]]; then
  cp "$PROJECT_DIR/examples/craft-kb.toml" "$EBOOKS_DIR/craft-kb.toml"
  ok "craft-kb.toml copié (titres et auteurs corrigés, modifiable)"
fi
"$UV" run --quiet --project "$PROJECT_DIR" craft-kb ingest "$EBOOKS_DIR" --prune

# ------------------------------------------------------------------ branchement
step "Branchement du serveur MCP"
if command -v claude >/dev/null; then
  if claude mcp get craft-kb >/dev/null 2>&1; then
    ok "Claude Code : déjà configuré"
  else
    claude mcp add --scope user craft-kb -- "$UV" --directory "$PROJECT_DIR" run craft-kb serve >/dev/null
    ok "Claude Code : craft-kb ajouté"
  fi
else
  warn "Claude Code absent : étape ignorée"
fi

if [[ -d "$(dirname "$DESKTOP_CONFIG")" ]]; then
  result="$("$UV" run --quiet --project "$PROJECT_DIR" python - "$DESKTOP_CONFIG" "$UV" "$PROJECT_DIR" <<'PY'
import json, shutil, sys
from pathlib import Path

config, uv, project = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
data = json.loads(config.read_text(encoding="utf-8")) if config.exists() and config.read_text().strip() else {}
entry = {"command": uv, "args": ["--directory", project, "run", "craft-kb", "serve"]}
servers = data.setdefault("mcpServers", {})
if servers.get("craft-kb") == entry:
    print("unchanged")
else:
    if config.exists():
        shutil.copy2(config, config.with_name(config.name + ".bak"))
    servers["craft-kb"] = entry
    config.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("updated")
PY
)"
  if [[ "$result" == "updated" ]]; then
    ok "Claude Desktop : craft-kb ajouté (sauvegarde en .bak) — redémarre l'application"
  else
    ok "Claude Desktop : déjà configuré"
  fi
else
  warn "Claude Desktop absent : étape ignorée"
fi

# ---------------------------------------------------------------------- bilan
step "Bilan"
"$UV" run --quiet --project "$PROJECT_DIR" craft-kb status
printf '\nPour ajouter un livre : dépose-le dans %s puis relance ./scripts/setup.sh\n' "$EBOOKS_DIR"
