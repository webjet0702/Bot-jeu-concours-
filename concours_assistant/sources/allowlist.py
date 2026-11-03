"""Allowlist : URLs de concours ajoutées à la main (une par ligne : `URL | nom facultatif`)."""

from __future__ import annotations

from pathlib import Path

from ..models import ConcoursCandidat
from ..urls import domaine


def lire_allowlist(chemin: Path) -> list[ConcoursCandidat]:
    if not chemin.exists():
        return []
    candidats = []
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        url, _, nom = (p.strip() for p in ligne.partition("|"))
        if not url.startswith(("http://", "https://")):
            continue
        candidats.append(ConcoursCandidat(url=url, nom=nom or f"Concours {domaine(url)}", source="allowlist"))
    return candidats
