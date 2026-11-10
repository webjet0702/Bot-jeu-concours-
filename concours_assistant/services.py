"""Opérations métier partagées par la ligne de commande et le site local.

Toutes les règles (robots.txt, débit, filtres, conformité, validation humaine)
vivent dans les modules appelés ici : le site n'a AUCUN chemin qui les contourne.
"""

from __future__ import annotations

import email
import json
import sqlite3
import time
from collections.abc import Callable
from datetime import date
from email.policy import default as politique_defaut

from .analyse import AReporter, analyser, enregistrer_decision
from .config import Config
from .conformite import evaluer
from .db import BLOCAGE_HUMAIN, Depot, maintenant
from .detection import DEFI_ACCES
from .http_poli import ClientPoli, ErreurAcces, QuotaAtteint
from .journal import Journal
from .models import ConcoursCandidat, Contexte, Decision, InfosExtraites, Mode, Statut
from .prefill import ResultatParticipation
from .sources.allowlist import lire_allowlist
from .sources.newsletter import collecter_newsletters, liens_depuis_email
from .sources.rss import collecter_flux
from .urls import domaine

Log = Callable[[str], None]
STATUTS_FORCABLES = (Statut.PRET.value, Statut.A_VERIFIER.value, Statut.ABANDONNE.value, Statut.NOUVEAU.value)


def collecter_tout(cfg: Config, depot: Depot, journal: Journal, mdp_imap: str | None, log: Log) -> tuple[int, int]:
    """RSS + Google Alertes/newsletters + allowlist. Renvoie (nouveaux, doublons)."""
    client = ClientPoli(cfg.reseau, depot, journal)
    candidats: list[ConcoursCandidat] = []
    for flux in cfg.collecte.flux:
        try:
            trouves = collecter_flux(flux, client)
            candidats += trouves
            log(f"Flux « {flux.nom} » : {len(trouves)} entrée(s)")
        except QuotaAtteint as e:
            log(f"⚠ {e.message}")
            if e.global_:
                break
        except ErreurAcces as e:
            log(f"⚠ Flux « {flux.nom} » ignoré : {e.message}")
            journal.evenement("flux_ignore", domaine=domaine(flux.url), raison=e.message)

    allow = lire_allowlist(cfg.chemin_allowlist)
    candidats += allow
    log(f"Liens ajoutés à la main : {len(allow)}")

    nl = cfg.collecte.newsletter
    if nl.active:
        if not mdp_imap:
            log("⚠ Google Alertes / newsletters : mot de passe d'application manquant, ignorées")
        else:
            try:
                trouves = collecter_newsletters(nl, mdp_imap)
                candidats += trouves
                log(f"Google Alertes / newsletters : {len(trouves)} lien(s)")
            except Exception as e:  # IMAP indisponible, mauvais mot de passe… : on continue sans
                log(f"⚠ Google Alertes / newsletters : {type(e).__name__}: {e}")
                journal.evenement("newsletter_erreur", erreur=type(e).__name__)

    nouveaux = sum(1 for c in candidats if depot.ajouter_concours(c) is not None)
    doublons = len(candidats) - nouveaux
    journal.evenement("collecte", candidats=len(candidats), nouveaux=nouveaux, doublons=doublons)
    log(f"✓ {nouveaux} nouveau(x) concours, {doublons} doublon(s) ignoré(s)")
    return nouveaux, doublons


def importer_emails(depot: Depot, journal: Journal, bruts: list[str]) -> tuple[int, int, int]:
    """Emails bruts (RFC 822) déposés par le script Gmail de l'utilisateur (Apps Script, sans mot de passe).

    Même extraction que la lecture IMAP : liens déballés, liens de gestion Google écartés, doublons ignorés.
    Rien n'est visité ici : l'analyse (robots.txt, débit…) se fait ensuite comme pour les autres sources.
    Renvoie (nouveaux, doublons, emails illisibles).
    """
    candidats: list[ConcoursCandidat] = []
    illisibles = 0
    for brut in bruts:
        try:
            candidats += liens_depuis_email(email.message_from_string(brut, policy=politique_defaut))
        except Exception:  # email mal formé : on l'ignore, les autres passent
            illisibles += 1
    nouveaux = sum(1 for c in candidats if depot.ajouter_concours(c) is not None)
    doublons = len(candidats) - nouveaux
    journal.evenement("import_gmail", emails=len(bruts), candidats=len(candidats), nouveaux=nouveaux,
                      doublons=doublons, illisibles=illisibles)
    return nouveaux, doublons, illisibles


def compter_en_attente(depot: Depot) -> int:
    return depot.compter_par_statut().get(Statut.NOUVEAU.value, 0)


def analyser_tout(cfg: Config, depot: Depot, journal: Journal, log: Log, *, ids: list[int] | None = None,
                  limite: int = 20, reessayer: bool = False, aujourd_hui: date | None = None,
                  budget_s: float | None = None) -> int:
    """Analyse polie des concours. Renvoie le nombre de concours analysés.

    `budget_s` : n'entame plus de nouveau concours au-delà de ce temps (requêtes web limitées en durée).
    Les concours non traités restent « nouveaux » et seront analysés au prochain passage.
    """
    aujourd_hui = aujourd_hui or date.today()
    debut = time.monotonic()
    client = ClientPoli(cfg.reseau, depot, journal)
    if ids:
        rows = [r for r in (depot.obtenir(i) for i in ids) if r is not None]
    else:
        statuts = [Statut.NOUVEAU.value] + ([Statut.ERREUR.value] if reessayer else [])
        rows = depot.lister(statuts)[:limite]
    if not rows:
        log("Rien à analyser.")
        return 0
    faits = 0
    for i, row in enumerate(rows, 1):
        if budget_s is not None and faits and time.monotonic() - debut > budget_s:
            log("… suite au prochain passage")
            break
        if row["statut"] == Statut.INSCRIT.value:  # jamais de réanalyse : on ne s'inscrit pas deux fois
            log(f"[{i}/{len(rows)}] déjà inscrit, ignoré : {row['nom'][:60]}")
            continue
        log(f"[{i}/{len(rows)}] {row['nom'][:70]}…")
        try:
            d = analyser(row, client=client, depot=depot, journal=journal, cfg=cfg, aujourd_hui=aujourd_hui)
        except AReporter as e:
            log(f"   reporté : {e}")
            if e.global_:
                break
            continue
        faits += 1
        log(f"   → {d.statut.value} ({d.mode.value})" + (f" : {d.motifs[0]}" if d.motifs else ""))
    log(f"✓ {faits} concours analysé(s)")
    return faits


def ajouter_lien(cfg: Config, depot: Depot, journal: Journal, url: str, nom: str = "",
                 ecrire_allowlist: bool = True) -> int | None:
    """Ajoute un lien trouvé à la main (base + allowlist.txt en local). Renvoie None si doublon."""
    url, nom = url.strip(), " ".join(nom.split())
    if not url.startswith(("http://", "https://")) or any(c in url for c in " \n\r|"):
        raise ValueError("le lien doit commencer par https:// (ou http://) et ne pas contenir d'espace")
    candidat = ConcoursCandidat(url=url, nom=nom or f"Concours {domaine(url)}", source="allowlist")
    cid = depot.ajouter_concours(candidat)
    if cid is not None:
        if ecrire_allowlist:
            with cfg.chemin_allowlist.open("a", encoding="utf-8") as fh:
                fh.write(f"{url} | {candidat.nom}\n" if nom else f"{url}\n")
        journal.evenement("lien_ajoute", concours_id=cid, domaine=domaine(url))
    return cid


def corriger(cfg: Config, depot: Depot, journal: Journal, row: sqlite3.Row, *, valeur: float | None = None,
             dotation: float | None = None, date_limite: str | None = None, age_min: int | None = None,
             reglement: str | None = None, aujourd_hui: date | None = None) -> Decision | None:
    """Correction humaine d'une extraction, puis réévaluation avec les MÊMES règles."""
    cond = json.loads(row["conditions"] or "{}")
    infos = InfosExtraites.depuis_dict(cond.get("infos", {}))
    ctx = Contexte.depuis_dict(cond.get("contexte", {}))
    modifs: dict = {}
    if valeur is not None:
        infos.gain_principal_eur = modifs["gain_principal_eur"] = valeur
    if dotation is not None:
        infos.dotation_totale_eur = modifs["dotation_totale_eur"] = dotation
        infos.dotation_estimee = False
    if date_limite:
        infos.date_limite = date.fromisoformat(date_limite)
        modifs["date_limite"] = date_limite
    if age_min is not None:
        infos.age_minimum = modifs["age_minimum"] = age_min or None
    if reglement:
        if not reglement.startswith(("http://", "https://")):
            raise ValueError("le lien du règlement doit commencer par https://")
        infos.url_reglement = modifs["url_reglement"] = reglement
        ctx.reglement_trouve = ctx.reglement_lu = True
    if not modifs:
        return None
    url = row["url_participation"] or row["url"]
    ctx.domaine_automatisation_autorisee = depot.domaine_autorise(domaine(url))
    d = evaluer(infos, ctx, cfg.profil, cfg.filtres, aujourd_hui or date.today())
    enregistrer_decision(depot, row["id"], infos, ctx, d, url)
    journal.evenement("correction_manuelle", concours_id=row["id"], champs=modifs, statut=d.statut.value)
    return d


def forcer_statut(depot: Depot, journal: Journal, row: sqlite3.Row, nouveau: str, motif: str) -> None:
    """Changement de statut après vérification humaine. Les contrôles durs restent appliqués à l'inscription."""
    if row["statut"] == Statut.INSCRIT.value:
        raise ValueError("déjà inscrit : statut non modifiable")
    if nouveau not in STATUTS_FORCABLES:
        raise ValueError("statut non autorisé")
    if not motif.strip():
        raise ValueError("indiquez ce que vous avez vérifié")
    champs: dict = {"statut": nouveau}
    if nouveau == Statut.PRET.value and row["mode_assistance"] in (None, Mode.ABSTENTION.value):
        champs["mode_assistance"] = Mode.MANUEL.value
    depot.mettre_a_jour(row["id"], **champs)
    journal.evenement("statut_force", concours_id=row["id"], ancien=row["statut"], nouveau=nouveau, motif=motif)


def enregistrer_resultat(depot: Depot, journal: Journal, row: sqlite3.Row, mode: Mode, url: str,
                         res: ResultatParticipation, abandonner: bool = False) -> None:
    if res.arret and res.arret.type == DEFI_ACCES:
        depot.bloquer_domaine(domaine(url), BLOCAGE_HUMAIN, f"défi anti-robot ({res.arret.fournisseur})")
    if res.envoye:
        depot.mettre_a_jour(row["id"], statut=Statut.INSCRIT.value, date_inscription=maintenant(), preuve=res.preuve)
        journal.evenement("inscription_confirmee", concours_id=row["id"], domaine=domaine(url), mode=mode.value,
                          preuve=res.preuve)
    else:
        journal.evenement("inscription_non_envoyee", concours_id=row["id"])
        if abandonner:
            depot.mettre_a_jour(row["id"], statut=Statut.ABANDONNE.value)
