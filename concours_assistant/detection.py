"""Détection des défis anti-robot et des CAPTCHA.

Ce module sert UNIQUEMENT à S'ARRÊTER : quand il détecte quelque chose, l'outil
interrompt toute action automatisée, journalise et rend la main à l'humain.
Il n'existe (et n'existera) aucun code pour résoudre, contourner ou éviter ces défis.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFI_ACCES = "defi_acces"                  # page interstitielle / blocage : on ne lit même pas le contenu
CAPTCHA_FORMULAIRE = "captcha_formulaire"  # widget CAPTCHA dans le formulaire : pas d'automatisation


@dataclass(frozen=True)
class Detection:
    type: str
    fournisseur: str


# Marqueurs de pages de challenge (interstitiels qui bloquent l'accès au contenu).
_INTERSTITIELS: list[tuple[str, re.Pattern]] = [
    ("Cloudflare", re.compile(r"/cdn-cgi/challenge-platform|cf-chl-|cf_chl_|<title>\s*just a moment|"
                              r"attention required!\s*\|\s*cloudflare|cf-browser-verification", re.I)),
    ("DataDome", re.compile(r"captcha-delivery\.com|geo\.captcha|datadome\s+captcha", re.I)),
    ("PerimeterX / HUMAN", re.compile(r"px-captcha|_pxCaptcha|perimeterx", re.I)),
    ("Imperva / Incapsula", re.compile(r"_incapsula_resource|incapsula incident", re.I)),
    ("Akamai", re.compile(r"akamai.{0,200}(?:access denied|reference\s*#)|"
                          r"(?:access denied|reference\s*#).{0,200}akamai", re.I | re.S)),
    ("Arkose Labs", re.compile(r"arkoselabs\.com|funcaptcha", re.I)),
]

# Textes typiques d'une page de vérification (FR/EN).
_TEXTES_VERIFICATION = re.compile(
    r"v[ée]rifi(?:ez|er|ons)\s+que\s+vous\s+(?:[êe]tes|n['’][êe]tes\s+pas)|"
    r"verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human|are\s+you\s+a\s+robot|[êe]tes[- ]vous\s+un\s+robot|"
    r"unusual\s+traffic|trafic\s+inhabituel|checking\s+your\s+browser|v[ée]rification\s+de\s+(?:votre\s+)?navigateur|"
    r"bot\s+detected|robot\s+d[ée]tect[ée]|access\s+denied|acc[èe]s\s+refus[ée]|request\s+blocked|requ[êe]te\s+bloqu[ée]e",
    re.I,
)

# Widgets CAPTCHA intégrés dans un formulaire.
_WIDGETS: list[tuple[str, re.Pattern]] = [
    ("reCAPTCHA", re.compile(r"g-recaptcha|google\.com/recaptcha|recaptcha/(?:api|enterprise)\.js|grecaptcha", re.I)),
    ("hCaptcha", re.compile(r"h-captcha|hcaptcha\.com", re.I)),
    ("Cloudflare Turnstile", re.compile(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I)),
    ("Friendly Captcha", re.compile(r"frc-captcha|friendlycaptcha", re.I)),
    ("CAPTCHA", re.compile(r"captcha", re.I)),  # filet de sécurité générique
]

_URL_CADRES_DEFI = re.compile(
    r"recaptcha|hcaptcha|challenges\.cloudflare\.com|captcha-delivery\.com|arkoselabs|funcaptcha|captcha", re.I)


def _entete(entetes: Mapping[str, str], nom: str) -> str:
    for k, v in entetes.items():
        if k.lower() == nom:
            return str(v)
    return ""


def detecter_defi(statut: int | None, entetes: Mapping[str, str], html: str) -> Detection | None:
    """Analyse une réponse HTTP (ou le HTML d'une page) et signale tout défi anti-robot."""
    html = html or ""
    if _entete(entetes, "cf-mitigated").lower() == "challenge":
        return Detection(DEFI_ACCES, "Cloudflare")

    for fournisseur, motif in _INTERSTITIELS:
        if motif.search(html):
            return Detection(DEFI_ACCES, fournisseur)

    if statut is not None and statut in (401, 403, 429, 503):
        serveur = (_entete(entetes, "server") + " " + _entete(entetes, "x-datadome")).lower()
        if "cloudflare" in serveur or "datadome" in serveur or "akamai" in serveur or _TEXTES_VERIFICATION.search(html):
            return Detection(DEFI_ACCES, "blocage anti-robot (HTTP %d)" % statut)

    # Page de vérification servie avec un code 200.
    if _TEXTES_VERIFICATION.search(html[:20000]) and len(html) < 60000 and re.search(r"captcha|challenge", html, re.I):
        return Detection(DEFI_ACCES, "page de vérification")

    for fournisseur, motif in _WIDGETS:
        if motif.search(html):
            return Detection(CAPTCHA_FORMULAIRE, fournisseur)
    return None


def detecter_cadres(urls_cadres: list[str]) -> Detection | None:
    """Iframes de CAPTCHA/challenge chargées dans la page (navigateur)."""
    for u in urls_cadres:
        if u and _URL_CADRES_DEFI.search(u):
            return Detection(CAPTCHA_FORMULAIRE, "CAPTCHA (iframe)")
    return None
