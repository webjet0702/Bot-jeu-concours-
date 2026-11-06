"""Modification ciblée de config.toml (on garde les commentaires et le reste du fichier)."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from ..config import Config, charger_config


def valeur_toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)  # une chaîne JSON est une chaîne TOML « basique » valide


def remplacer_section(texte: str, section: str, valeurs: dict) -> str:
    lignes = texte.splitlines()
    entete = f"[{section}]"
    try:
        debut = next(i for i, ligne in enumerate(lignes) if ligne.strip() == entete)
    except StopIteration:
        bloc = ["", entete] + [f"{k} = {valeur_toml(v)}" for k, v in valeurs.items()]
        return "\n".join(lignes + bloc) + "\n"
    fin = next((i for i in range(debut + 1, len(lignes)) if lignes[i].lstrip().startswith("[")), len(lignes))
    restants = dict(valeurs)
    for i in range(debut + 1, fin):
        for cle in list(restants):
            if re.match(rf"^\s*#?\s*{re.escape(cle)}\s*=", lignes[i]):
                lignes[i] = f"{cle} = {valeur_toml(restants.pop(cle))}"
                break
    if restants:
        insertion = fin
        while insertion > debut + 1 and not lignes[insertion - 1].strip():
            insertion -= 1
        lignes[insertion:insertion] = [f"{k} = {valeur_toml(v)}" for k, v in restants.items()]
    return "\n".join(lignes) + "\n"


def enregistrer_config(chemin: Path, sections: dict[str, dict]) -> Config:
    """Applique les modifications, VALIDE le résultat, puis remplace le fichier. Lève ErreurConfig sinon."""
    texte = chemin.read_text(encoding="utf-8")
    for section, valeurs in sections.items():
        texte = remplacer_section(texte, section, valeurs)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=chemin.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(texte)
        cfg = charger_config(tmp)  # mêmes garde-fous que d'habitude (seuil >= 100 €, UA honnête…)
        os.replace(tmp, chemin)
        return cfg
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
