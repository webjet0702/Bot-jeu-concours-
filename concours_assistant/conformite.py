"""Décision (filtres + conformité) et checklist avant chaque inscription."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from .config import Filtres, Profil
from .db import Depot
from .extraction import pays_compatible
from .models import Contexte, Decision, InfosExtraites, Mode, Statut
from .urls import domaine


def evaluer(infos: InfosExtraites, ctx: Contexte, profil: Profil, filtres: Filtres,
            aujourd_hui: date) -> Decision:
    if ctx.doublon_de:
        return Decision(Statut.EXCLU, Mode.ABSTENTION, [f"doublon du concours #{ctx.doublon_de}"])

    exclusions: list[str] = []   # => EXCLU (règle dure)
    humain: list[str] = []       # => INTERVENTION_HUMAINE
    verifs: list[str] = []       # => A_VERIFIER
    avert: list[str] = []        # informatif, affiché dans la checklist

    # --- Accès au site
    if ctx.page_introuvable:
        exclusions.append("page du concours introuvable (404/410)")
    if not ctx.https:
        exclusions.append("page non chiffrée (HTTP) : aucune donnée personnelle ne doit y être envoyée")
    if ctx.defi_acces:
        humain.append(f"défi anti-robot détecté ({ctx.defi_acces}) : automatisation arrêtée, vérifiez vous-même")
    if ctx.acces_refuse:
        humain.append("accès refusé par le site (401/403) : automatisation arrêtée, vérifiez vous-même")
    if ctx.domaine_bloque:
        humain.append(f"domaine bloqué ({ctx.domaine_bloque}) : levez le blocage après vérification humaine")
    if not ctx.robots_autorise:
        verifs.append("robots.txt interdit l'analyse automatique : lisez le concours vous-même")
    if ctx.reseau_social:
        verifs.append("concours sur réseau social : aucune automatisation ; vérifiez que le compte est officiel "
                      "(badge, ancienneté, abonnés) — les faux comptes sont la 1re source d'arnaques")

    # --- Valeur (règle dure : >= seuil)
    valeur = infos.valeur_retenue(filtres.critere_valeur)
    if valeur is None:
        verifs.append("valeur du gain non déterminée : renseignez-la avec `concours corriger`")
    elif valeur < filtres.valeur_minimale_eur:
        exclusions.append(f"gain {valeur:.0f} € < seuil de {filtres.valeur_minimale_eur:.0f} €")
    if infos.dotation_estimee and filtres.critere_valeur == "dotation_totale":
        avert.append("dotation totale estimée (somme des lots trouvés) : à confirmer dans le règlement")

    # --- Date limite
    if infos.date_limite is None:
        avert.append("date limite non trouvée : vérifiez-la dans le règlement")
    else:
        restant = (infos.date_limite - aujourd_hui).days
        if restant < 0:
            exclusions.append(f"date limite dépassée ({infos.date_limite.isoformat()})")
        elif restant < filtres.jours_restants_minimum:
            exclusions.append(f"se termine dans {restant} jour(s) (< {filtres.jours_restants_minimum})")

    # --- Âge
    age = profil.age_effectif(aujourd_hui)
    if infos.age_minimum:
        if age is None:
            verifs.append(f"âge minimum {infos.age_minimum} ans : renseignez votre âge dans config.toml")
        elif age < infos.age_minimum:
            if infos.autorisation_parentale:
                verifs.append(f"réservé aux {infos.age_minimum} ans et plus, sauf autorisation parentale : "
                              "participez uniquement avec cette autorisation écrite")
            else:
                exclusions.append(f"réservé aux {infos.age_minimum} ans et plus (vous avez {age} ans)")

    # --- Pays de résidence
    if not pays_compatible(infos.pays_autorises, profil.pays_residence):
        exclusions.append(f"réservé aux résidents de : {', '.join(infos.pays_autorises)} "
                          f"(vous : {profil.pays_residence})")
    elif not infos.pays_autorises and ctx.reglement_lu:
        avert.append("pays de résidence exigé non trouvé : vérifiez que le concours est ouvert à votre pays")

    # --- Arnaque / achat
    for s in infos.signaux_arnaque:
        exclusions.append(f"signal d'arnaque (paiement/coordonnées bancaires) {s}")
    if infos.achat_obligatoire:
        exclusions.append("participation conditionnée à un achat")

    # --- Règlement / organisateur
    analyse_possible = ctx.robots_autorise and not (ctx.reseau_social or ctx.defi_acces or ctx.acces_refuse
                                                    or ctx.domaine_bloque)
    if analyse_possible:
        if not ctx.reglement_trouve:
            verifs.append("règlement introuvable : ne participez pas sans l'avoir lu")
        elif not ctx.reglement_lu:
            avert.append("règlement trouvé mais non analysé (PDF illisible, robots.txt ou erreur) : lisez-le")
        if not infos.organisateur_identifie:
            avert.append("organisateur non identifié (aucun SIREN/RCS trouvé)")
    if infos.depot_commissaire:
        avert.append("✓ règlement déposé chez un commissaire de justice (bon signe)")

    # --- Mode d'assistance
    raisons_manuel: list[str] = []
    if infos.interdit_automatisation:
        raisons_manuel.append("le règlement interdit les moyens automatisés")
    if ctx.captcha_formulaire:
        raisons_manuel.append(f"CAPTCHA présent dans le formulaire ({ctx.captcha_formulaire})")
    if not ctx.domaine_automatisation_autorisee:
        raisons_manuel.append("domaine non autorisé pour l'assistance automatisée "
                              "(`concours domaine autoriser` après lecture des CGU)")
    if not analyse_possible:
        raisons_manuel.append("site non analysable automatiquement")

    motifs = exclusions + humain + verifs
    if exclusions:
        return Decision(Statut.EXCLU, Mode.ABSTENTION, motifs, avert)
    mode = Mode.MANUEL if raisons_manuel else Mode.PREREMPLISSAGE
    avert += [f"mode manuel : {r}" for r in raisons_manuel]
    if humain:
        return Decision(Statut.INTERVENTION_HUMAINE, Mode.MANUEL, motifs, avert)
    if verifs:
        return Decision(Statut.A_VERIFIER, mode, motifs, avert)
    return Decision(Statut.PRET, mode, motifs, avert)


# --------------------------------------------------------------------------
# Checklist exécutée juste avant chaque inscription


@dataclass
class PointControle:
    libelle: str
    ok: bool | None  # None = à confirmer par l'humain
    detail: str = ""


def checklist_avant_inscription(row: sqlite3.Row, profil: Profil, filtres: Filtres, depot: Depot,
                                aujourd_hui: date, version_consentement: str) -> list[PointControle]:
    url = row["url_participation"] or row["url"]
    d = domaine(url)
    gain = row["gain_principal_eur"] if filtres.critere_valeur == "lot_principal" else (
        row["dotation_totale_eur"] if row["dotation_totale_eur"] is not None else row["gain_principal_eur"])
    champs = ", ".join(profil.champs_transmissibles()) or "(aucun)"
    points = [
        PointControle("Consentement au traitement des données enregistré",
                      depot.consentement_actif(version_consentement)),
        PointControle("Concours au statut « prêt »", row["statut"] == Statut.PRET.value, row["statut"]),
        PointControle("Pas déjà inscrit", row["date_inscription"] is None),
        PointControle(f"Gain ≥ {filtres.valeur_minimale_eur:.0f} €",
                      gain is not None and gain >= filtres.valeur_minimale_eur,
                      f"{gain:.0f} €" if gain is not None else "inconnu"),
        PointControle("Domaine sans blocage ni défi anti-robot en cours", depot.domaine_bloque(d) is None,
                      depot.domaine_bloque(d) or d),
        PointControle("Connexion chiffrée (HTTPS)", url.startswith("https://")),
        PointControle("Mode d'assistance défini", row["mode_assistance"] in (Mode.PREREMPLISSAGE, Mode.MANUEL),
                      row["mode_assistance"] or "aucun"),
        PointControle(f"Données minimales : seuls ces champs seront proposés : {champs}", True),
    ]
    if row["date_limite"]:
        fin = date.fromisoformat(row["date_limite"])
        points.append(PointControle("Date limite non dépassée", fin >= aujourd_hui, row["date_limite"]))
    else:
        points.append(PointControle("La date limite n'est pas dépassée (non trouvée automatiquement)", None))
    if row["mode_assistance"] == Mode.PREREMPLISSAGE:
        points.append(PointControle("Assistance automatisée autorisée pour ce domaine", depot.domaine_autorise(d), d))

    reglement = row["url_reglement"] or "lien non trouvé : cherchez-le sur la page"
    points += [
        PointControle(f"J'ai lu le règlement ({reglement})", None),
        PointControle("L'organisateur est légitime (site/compte officiel) et aucun paiement n'est demandé", None),
        PointControle("Je remplis les conditions (âge, pays de résidence, une participation par personne/foyer)", None),
        PointControle("J'accepte que les champs ci-dessus soient transmis à l'organisateur pour CE concours", None),
    ]
    return points
