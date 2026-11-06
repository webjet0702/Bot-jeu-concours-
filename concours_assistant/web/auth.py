"""Connexion à la version en ligne : un seul utilisateur (toi), mot de passe haché, anti force brute."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from ..db import Depot, maintenant

CLE_MOT_DE_PASSE = "mot_de_passe"
CLE_VERROU = "verrou_connexion"
ITERATIONS = 390_000
ECHECS_AVANT_BLOCAGE = 5
LONGUEUR_MIN = 10


def hacher(mot_de_passe: str, sel: bytes | None = None, iterations: int = ITERATIONS) -> str:
    sel = sel or secrets.token_bytes(16)
    empreinte = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode("utf-8"), sel, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, base64.b64encode(sel).decode(),
                                           base64.b64encode(empreinte).decode())


def verifier(mot_de_passe: str, stocke: str) -> bool:
    try:
        algo, iterations, sel, empreinte = stocke.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calcule = hacher(mot_de_passe, base64.b64decode(sel), int(iterations)).split("$")[3]
        return hmac.compare_digest(calcule, empreinte)
    except (ValueError, TypeError):
        return False


def code_installation() -> str | None:
    return (os.environ.get("CONCOURS_CODE_INSTALLATION") or "").strip() or None


def mot_de_passe_defini(depot: Depot) -> bool:
    return depot.lire_parametre(CLE_MOT_DE_PASSE) is not None


def valider_nouveau(mot_de_passe: str, confirmation: str) -> None:
    if len(mot_de_passe) < LONGUEUR_MIN:
        raise ValueError(f"au moins {LONGUEUR_MIN} caractères")
    if mot_de_passe != confirmation:
        raise ValueError("les deux mots de passe ne sont pas identiques")


def definir(depot: Depot, mot_de_passe: str) -> None:
    depot.ecrire_parametre(CLE_MOT_DE_PASSE, hacher(mot_de_passe))
    depot.supprimer_parametre(CLE_VERROU)


def _verrou(depot: Depot) -> dict:
    try:
        return json.loads(depot.lire_parametre(CLE_VERROU) or "{}")
    except ValueError:
        return {}


def bloque_jusqu_au(depot: Depot) -> str | None:
    v = _verrou(depot)
    return v["jusqu_au"] if v.get("jusqu_au") and v["jusqu_au"] > maintenant() else None


def tenter_connexion(depot: Depot, mot_de_passe: str) -> bool:
    if bloque_jusqu_au(depot):
        return False
    stocke = depot.lire_parametre(CLE_MOT_DE_PASSE)
    if stocke and verifier(mot_de_passe, stocke):
        depot.supprimer_parametre(CLE_VERROU)
        return True
    v = _verrou(depot)
    echecs = int(v.get("echecs", 0)) + 1
    jusqu_au = None
    if echecs >= ECHECS_AVANT_BLOCAGE:  # 15 min, puis double à chaque nouvel échec (max 24 h)
        minutes = min(15 * 2 ** (echecs - ECHECS_AVANT_BLOCAGE), 24 * 60)
        jusqu_au = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    depot.ecrire_parametre(CLE_VERROU, json.dumps({"echecs": echecs, "jusqu_au": jusqu_au}))
    return False
