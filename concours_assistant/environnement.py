"""Choix du mode : « local » (ton PC : SQLite + config.toml) ou « en ligne » (PostgreSQL + config en base).

Le mode en ligne est activé par la variable d'environnement CONCOURS_DATABASE_URL (une ou
plusieurs URL PostgreSQL séparées par des espaces, essayées dans l'ordre).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .config import Config, charger_config_texte, modele_config
from .db import Depot

CLE_CONFIG = "config_toml"


def url_base() -> str | None:
    valeur = (os.environ.get("CONCOURS_DATABASE_URL") or "").strip()
    return valeur or None


def en_ligne() -> bool:
    return url_base() is not None


def racine_en_ligne() -> Path:
    racine = Path(tempfile.gettempdir()) / "concours-assistant"
    racine.mkdir(parents=True, exist_ok=True)
    return racine


def ouvrir_depot(cfg: Config) -> Depot:
    return Depot(url_base() or cfg.chemin_base)


class SourceConfig:
    """Où vit la configuration : un fichier config.toml (local) ou la table `parametres` (en ligne)."""

    def __init__(self, chemin_fichier: Path | None = None):
        self.chemin_fichier = Path(chemin_fichier).resolve() if chemin_fichier and not en_ligne() else None

    @property
    def description(self) -> str:
        return str(self.chemin_fichier) if self.chemin_fichier else "ta base de données en ligne (Supabase)"

    def lire_texte(self) -> str:
        if self.chemin_fichier:
            return self.chemin_fichier.read_text(encoding="utf-8")
        depot = Depot(url_base())
        try:
            return depot.lire_parametre(CLE_CONFIG) or modele_config()
        finally:
            depot.fermer()

    def racine(self) -> Path:
        return self.chemin_fichier.parent if self.chemin_fichier else racine_en_ligne()

    def charger(self) -> Config:
        return charger_config_texte(self.lire_texte(), self.racine())

    def ecrire_texte(self, texte: str) -> Config:
        """Valide puis enregistre (ErreurConfig si invalide : rien n'est écrit)."""
        cfg = charger_config_texte(texte, self.racine())
        if self.chemin_fichier:
            tmp = self.chemin_fichier.with_name(f".{self.chemin_fichier.name}.tmp")
            tmp.write_text(texte, encoding="utf-8")
            os.replace(tmp, self.chemin_fichier)
        else:
            depot = Depot(url_base())
            try:
                depot.ecrire_parametre(CLE_CONFIG, texte)
            finally:
                depot.fermer()
        return cfg
