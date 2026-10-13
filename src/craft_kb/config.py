"""Configuration, lue depuis les variables d'environnement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    ollama_url: str
    embed_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        db = os.environ.get("CRAFT_KB_DB", str(Path.home() / ".craft-kb" / "kb.sqlite"))
        return cls(
            db_path=Path(db).expanduser(),
            ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            embed_model=os.environ.get("CRAFT_KB_EMBED_MODEL", "bge-m3"),
        )
