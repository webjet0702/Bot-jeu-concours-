"""Collecte depuis des flux RSS/Atom (via le client poli : robots.txt + débit)."""

from __future__ import annotations

import feedparser

from ..config import Flux
from ..extraction import texte_depuis_html
from ..http_poli import ClientPoli
from ..models import ConcoursCandidat

MAX_ENTREES_PAR_FLUX = 100


def collecter_flux(flux: Flux, client: ClientPoli) -> list[ConcoursCandidat]:
    reponse = client.get(flux.url)
    analyse = feedparser.parse(reponse.contenu)
    candidats = []
    for entree in analyse.entries[:MAX_ENTREES_PAR_FLUX]:
        lien = entree.get("link")
        titre = entree.get("title")
        if not lien or not titre or not lien.startswith(("http://", "https://")):
            continue
        morceaux = [entree.get("summary", "")]
        morceaux += [c.get("value", "") for c in entree.get("content", [])]
        texte = texte_depuis_html(" ".join(morceaux))
        candidats.append(ConcoursCandidat(url=lien, nom=titre, source=f"rss:{flux.nom}", texte=texte))
    return candidats
