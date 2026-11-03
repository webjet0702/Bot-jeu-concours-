"""Normalisation d'URL et empreintes pour la déduplication."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_PARAMS_TRACKING = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi|igshid|yclid|ref|ref_src|xtor)$",
    re.IGNORECASE,
)

RESEAUX_SOCIAUX = {
    "instagram.com", "facebook.com", "fb.com", "fb.me", "tiktok.com", "x.com",
    "twitter.com", "threads.net", "youtube.com", "snapchat.com", "linkedin.com",
    "pinterest.com", "pinterest.fr", "discord.com", "discord.gg", "t.me", "telegram.me",
}


def domaine(url: str) -> str:
    hote = (urlsplit(url).hostname or "").lower()
    return hote[4:] if hote.startswith("www.") else hote


def est_reseau_social(url: str) -> bool:
    d = domaine(url)
    return any(d == r or d.endswith("." + r) for r in RESEAUX_SOCIAUX)


def canonicaliser(url: str) -> str:
    """Forme canonique : hôte en minuscules, sans www, sans fragment ni paramètres de suivi."""
    parties = urlsplit(url.strip())
    schema = (parties.scheme or "https").lower()
    hote = (parties.hostname or "").lower()
    if hote.startswith("www."):
        hote = hote[4:]
    port = parties.port
    if port and not ((schema == "http" and port == 80) or (schema == "https" and port == 443)):
        hote = f"{hote}:{port}"
    chemin = re.sub(r"/{2,}", "/", parties.path or "/")
    if len(chemin) > 1:
        chemin = chemin.rstrip("/")
    params = sorted((k, v) for k, v in parse_qsl(parties.query, keep_blank_values=True)
                    if not _PARAMS_TRACKING.match(k))
    return urlunsplit((schema, hote, chemin, urlencode(params), ""))


def normaliser_texte(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte.lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", texte).strip()


def empreinte(url: str, nom: str) -> str:
    base = f"{domaine(url)}|{normaliser_texte(nom)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]
