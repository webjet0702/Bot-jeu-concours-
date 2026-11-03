"""Journal d'audit (table `journal` + logging). Les données personnelles sont masquées."""

from __future__ import annotations

import logging
import re

from .db import Depot

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TELEPHONE = re.compile(r"(?:\+33\s?|0)[1-9](?:[\s.-]?\d{2}){4}")

log = logging.getLogger("concours")


def masquer(valeur):
    if isinstance(valeur, str):
        return _TELEPHONE.sub("[tél. masqué]", _EMAIL.sub("[email masqué]", valeur))
    if isinstance(valeur, (list, tuple)):
        return [masquer(v) for v in valeur]
    if isinstance(valeur, dict):
        return {k: masquer(v) for k, v in valeur.items()}
    return valeur


class Journal:
    def __init__(self, depot: Depot):
        self.depot = depot

    def evenement(self, type_: str, *, concours_id: int | None = None, domaine: str | None = None,
                  **details) -> None:
        details = masquer(details)
        self.depot.ajouter_journal(type_, concours_id, domaine, details)
        log.info("%s concours=%s domaine=%s %s", type_, concours_id, domaine, details)
