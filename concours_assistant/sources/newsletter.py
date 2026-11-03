"""Newsletters de concours via IMAP, en LECTURE SEULE (BODY.PEEK : rien n'est marqué lu ni modifié)."""

from __future__ import annotations

import email
import imaplib
import re
from datetime import date, timedelta
from email.message import EmailMessage
from email.policy import default as politique_defaut

from bs4 import BeautifulSoup

from ..config import Newsletter
from ..models import ConcoursCandidat

_MOIS_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_LIEN_CONCOURS = re.compile(r"concours|jeu|gagn|tirage|à\s+gagner|particip|giveaway", re.I)
_LIEN_A_IGNORER = re.compile(r"d[ée]sinscri|unsubscribe|d[ée]sabonn|pr[ée]f[ée]rences|optout|opt-out|"
                             r"confidentialit|privacy|mentions\s+l[ée]gales|voir\s+(?:la\s+)?version\s+en\s+ligne", re.I)
MAX_MESSAGES = 200


def liens_depuis_email(message: EmailMessage) -> list[ConcoursCandidat]:
    sujet = str(message.get("subject", "")).strip()
    partie = message.get_body(preferencelist=("html",))
    if partie is None:
        return []
    soup = BeautifulSoup(partie.get_content(), "html.parser")
    candidats, vus = [], set()
    for a in soup.find_all("a", href=True):
        url = a["href"].strip()
        texte = a.get_text(" ", strip=True)
        if not url.startswith("https://") or url in vus:
            continue
        indice = f"{texte} {a.get('title') or ''} {url}"
        if _LIEN_A_IGNORER.search(indice) or not _LIEN_CONCOURS.search(indice):
            continue
        vus.add(url)
        candidats.append(ConcoursCandidat(url=url, nom=(texte or sujet)[:200], source="newsletter",
                                          texte=f"{sujet}. {texte}"))
    return candidats


def collecter_newsletters(cfg: Newsletter, mot_de_passe: str) -> list[ConcoursCandidat]:
    depuis = date.today() - timedelta(days=cfg.jours)
    critere = f"{depuis.day:02d}-{_MOIS_IMAP[depuis.month - 1]}-{depuis.year}"
    candidats: list[ConcoursCandidat] = []
    with imaplib.IMAP4_SSL(cfg.serveur_imap) as imap:
        imap.login(cfg.utilisateur, mot_de_passe)
        typ, _ = imap.select(f'"{cfg.dossier}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"dossier IMAP introuvable : {cfg.dossier}")
        typ, donnees = imap.search(None, "SINCE", critere)
        for num in (donnees[0].split() if donnees and donnees[0] else [])[-MAX_MESSAGES:]:
            typ, parts = imap.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            message = email.message_from_bytes(parts[0][1], policy=politique_defaut)
            candidats.extend(liens_depuis_email(message))
    return candidats
