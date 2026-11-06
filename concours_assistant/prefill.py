"""Pré-remplissage dans un navigateur VISIBLE, puis envoi par l'humain.

Ce que ce module fait :
- ouvre Chromium en mode visible (headless=False, non configurable) ;
- vérifie l'absence de CAPTCHA/défi AVANT chaque action ; s'il y en a un : arrêt
  immédiat de toute automatisation, journalisation, la main passe à l'humain ;
- remplit uniquement les champs texte reconnus sans ambiguïté, avec les seules
  données du profil (minimisation), via `fill()` (pas de simulation de frappe).

Ce qu'il ne fait JAMAIS :
- cocher une case (règlement, newsletter, partenaires) : c'est le consentement de l'humain ;
- cliquer sur « Valider » / soumettre le formulaire ;
- modifier l'empreinte du navigateur, le User-Agent, navigator.webdriver, utiliser un
  proxy, un plugin furtif ou toute autre technique pour masquer l'automatisation ;
- toucher à un CAPTCHA.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .detection import Detection, detecter_cadres, detecter_defi
from .journal import Journal
from .urls import domaine

SELECTEUR_CHAMPS = "input, textarea"
TYPES_IGNORES = {"hidden", "checkbox", "radio", "submit", "button", "reset", "image", "file", "password",
                 "range", "color", "search"}

# clé du profil -> (valeurs autocomplete, types HTML, jetons de nom/label)
REGLES: dict[str, tuple[set[str], set[str], set[str]]] = {
    "email": ({"email"}, {"email"}, {"email", "mail", "courriel", "emailaddress", "adressemail", "adresseemail"}),
    "prenom": ({"given-name"}, set(), {"prenom", "firstname", "fname", "givenname", "first"}),
    "nom": ({"family-name"}, set(), {"nom", "lastname", "lname", "familyname", "surname", "last", "nomdefamille"}),
    "code_postal": ({"postal-code"}, set(), {"codepostal", "cp", "postal", "zip", "zipcode", "postcode",
                                             "postalcode"}),
    "ville": ({"address-level2"}, set(), {"ville", "city", "town", "commune", "localite"}),
    "telephone": ({"tel", "tel-national"}, {"tel"}, {"telephone", "tel", "phone", "mobile", "portable", "gsm"}),
    "date_naissance": ({"bday"}, set(), {"naissance", "birthday", "birthdate", "dob", "bday", "datenaissance",
                                         "datedenaissance", "birth"}),
}

JS_DESCRIPTEURS = """
(els) => els.map((el, i) => {
  const r = el.getBoundingClientRect();
  const s = window.getComputedStyle(el);
  let label = '';
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = l.innerText; }
  if (!label) { const l = el.closest('label'); if (l) label = l.innerText; }
  return {
    index: i, tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || 'text').toLowerCase(),
    name: el.getAttribute('name') || '', id: el.id || '',
    autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
    placeholder: el.getAttribute('placeholder') || '', aria: el.getAttribute('aria-label') || '',
    label: label || '', visible: r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none',
    desactive: el.disabled || el.readOnly, rempli: !!el.value
  };
})
"""


def _jetons(*textes: str) -> set[str]:
    jetons: set[str] = set()
    for t in textes:
        if not t:
            continue
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
        t = unicodedata.normalize("NFKD", t.lower())
        t = "".join(c for c in t if not unicodedata.combining(c))
        mots = [m for m in re.split(r"[^a-z0-9]+", t) if m]
        jetons.update(mots)
        if 1 < len(mots) <= 4:
            jetons.add("".join(mots))
    return jetons


def cles_possibles(champ: dict) -> set[str]:
    ac = champ.get("autocomplete", "").split()
    ac_dernier = ac[-1] if ac else ""
    par_ac = {cle for cle, (acs, _, _) in REGLES.items() if ac_dernier in acs}
    if par_ac:
        return par_ac
    par_type = {cle for cle, (_, types, _) in REGLES.items() if champ.get("type") in types}
    if par_type:
        return par_type
    jetons = _jetons(champ.get("name", ""), champ.get("id", ""), champ.get("label", ""),
                     champ.get("placeholder", ""), champ.get("aria", ""))
    return {cle for cle, (_, _, motifs) in REGLES.items() if jetons & motifs}


def associer_champs(descripteurs: list[dict], valeurs: dict[str, str]) -> list[tuple[int, str]]:
    """Plan de remplissage : [(index du champ, clé du profil)]. Tout champ ambigu est laissé à l'humain."""
    candidats: dict[str, list[int]] = {}
    for champ in descripteurs:
        if (champ.get("type") in TYPES_IGNORES or not champ.get("visible") or champ.get("desactive")
                or champ.get("rempli")):
            continue
        cles = cles_possibles(champ)
        if len(cles) != 1:
            continue  # aucun ou plusieurs sens possibles (ex. « Nom et prénom ») => humain, quel que soit le profil
        cle = cles.pop()
        if cle in valeurs:  # minimisation : uniquement les champs renseignés dans le profil
            candidats.setdefault(cle, []).append(champ["index"])
    plan = []
    for cle, index in candidats.items():
        if len(index) == 1 or cle == "email":  # « email » + « confirmez l'email » : même valeur
            plan += [(i, cle) for i in index]
    return sorted(plan)


def formater(cle: str, valeur: str, type_champ: str) -> str:
    if cle == "date_naissance" and type_champ != "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", valeur):
        a, m, j = valeur.split("-")
        return f"{j}/{m}/{a}"
    return valeur


def affichage_disponible() -> bool:
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


@dataclass
class ResultatParticipation:
    envoye: bool
    preuve: str | None = None
    arret: Detection | None = None
    champs_remplis: tuple[str, ...] = ()


def _oui(reponse: str) -> bool:
    return reponse.strip().lower() in ("o", "oui", "y", "yes")


def detecter_sur_page(page) -> Detection | None:
    d = detecter_defi(None, {}, page.content())
    return d or detecter_cadres([f.url for f in page.frames])


INSTRUCTIONS = """
──────────────────────────────────────────────────────────────────────
 À VOUS DE JOUER dans la fenêtre du navigateur :
   1. Vérifiez les champs pré-remplis : {champs}
   2. Complétez ce qui manque, cochez VOUS-MÊME les cases (règlement, etc.)
   3. Si un CAPTCHA est présent, c'est à vous de le faire
   4. Cliquez VOUS-MÊME sur « Valider » / « Participer »
 L'outil ne clique sur rien et ne soumet rien.
──────────────────────────────────────────────────────────────────────"""


def preremplir_et_attendre(url: str, valeurs: dict[str, str], *, concours_id: int, journal: Journal,
                           dossier_preuves: Path, domaine_autorise: Callable[[str], bool],
                           demander: Callable[[str], str] = input,
                           notifier: Callable[[str], None] = print) -> ResultatParticipation:
    from playwright.sync_api import Error as ErreurPlaywright
    from playwright.sync_api import sync_playwright

    dossier_preuves.mkdir(parents=True, exist_ok=True)
    arret: Detection | None = None
    remplis: list[str] = []
    with sync_playwright() as p:
        navigateur = p.chromium.launch(headless=False)  # toujours visible ; aucune option furtive
        try:
            page = navigateur.new_page()
            notifier("Navigateur ouvert, chargement de la page du concours…")
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            arret = detecter_sur_page(page)
            if arret is None:
                cadres = [page.main_frame] + [f for f in page.frames
                                              if f is not page.main_frame and f.url.startswith("https://")
                                              and domaine_autorise(domaine(f.url))]
                for cadre in cadres:
                    descripteurs = cadre.eval_on_selector_all(SELECTEUR_CHAMPS, JS_DESCRIPTEURS)
                    types = {c["index"]: c["type"] for c in descripteurs}
                    champs = cadre.locator(SELECTEUR_CHAMPS)
                    for index, cle in associer_champs(descripteurs, valeurs):
                        arret = detecter_sur_page(page)  # contrôle avant CHAQUE action
                        if arret:
                            break
                        champs.nth(index).fill(formater(cle, valeurs[cle], types[index]))
                        remplis.append(cle)
                    if arret:
                        break
            if arret:
                journal.evenement("captcha_detecte" if arret.type == "captcha_formulaire" else "defi_anti_bot",
                                  concours_id=concours_id, domaine=domaine(url), fournisseur=arret.fournisseur,
                                  action="automatisation arrêtée, main rendue à l'humain")
                notifier(f"⛔ {arret.fournisseur} détecté : l'automatisation est ARRÊTÉE. "
                         "Terminez vous-même dans le navigateur (ou abandonnez).")
            journal.evenement("preremplissage_termine", concours_id=concours_id, domaine=domaine(url),
                              champs=sorted(set(remplis)), arret=bool(arret))
            notifier(INSTRUCTIONS.format(champs=", ".join(sorted(set(remplis))) or "aucun"))
            demander("\nAppuyez sur Entrée quand vous avez terminé (envoyé ou abandonné)… ")
            if not _oui(demander("Avez-vous VOUS-MÊME validé et l'inscription est-elle envoyée ? [o/N] ")):
                return ResultatParticipation(False, arret=arret, champs_remplis=tuple(remplis))
            preuve = dossier_preuves / f"concours_{concours_id}_{datetime.now():%Y%m%d_%H%M%S}.png"
            try:
                page.screenshot(path=str(preuve), full_page=True)
                chemin = str(preuve)
            except ErreurPlaywright:
                chemin = "confirmé par l'utilisateur (capture impossible : page fermée)"
            return ResultatParticipation(True, preuve=chemin, arret=arret, champs_remplis=tuple(remplis))
        finally:
            navigateur.close()


def participation_manuelle(url: str, valeurs: dict[str, str], *, demander: Callable[[str], str] = input,
                           ouvrir: Callable[[str], object] = webbrowser.open) -> ResultatParticipation:
    print("\nMode MANUEL : le lien s'ouvre dans votre navigateur habituel, l'outil n'y fait rien.")
    print("Données à saisir (minimales) :")
    for cle, valeur in valeurs.items():
        print(f"   {cle:15} {valeur}")
    print(f"   {'lien':15} {url}")
    ouvrir(url)
    if not _oui(demander("\nInscription envoyée par vous ? [o/N] ")):
        return ResultatParticipation(False)
    note = demander("Preuve (ex. « email de confirmation reçu », chemin d'une capture) [Entrée = aucune] : ")
    return ResultatParticipation(True, preuve=f"manuel : {note.strip() or 'confirmé par l’utilisateur'}")
