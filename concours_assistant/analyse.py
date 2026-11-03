"""Analyse d'un concours : récupération polie -> extraction -> filtres -> conformité -> statut."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .conformite import evaluer
from .db import Depot, maintenant
from .detection import CAPTCHA_FORMULAIRE
from .extraction import extraire_infos, lien_participation, liens_reglement, texte_depuis_html, texte_depuis_pdf
from .http_poli import (AccesRefuse, ClientPoli, DefiAntiBot, DomaineBloque, ErreurAcces, ErreurReseau,
                        LimiteDebit, PageIntrouvable, QuotaAtteint, Reponse, RobotsInterdit)
from .journal import Journal
from .models import Contexte, Decision, InfosExtraites, Mode, Statut
from .urls import canonicaliser, domaine, est_reseau_social


class AReporter(Exception):
    """Analyse impossible maintenant (quota, limite de débit) : on réessaiera plus tard."""

    def __init__(self, raison: str, global_: bool = False):
        super().__init__(raison)
        self.global_ = global_


@dataclass
class _Etat:
    ctx: Contexte = field(default_factory=Contexte)
    erreur: str | None = None


def _recuperer(client: ClientPoli, url: str, etat: _Etat, principal: bool) -> Reponse | None:
    """Récupère une URL et traduit chaque refus en contexte. Ne contourne jamais rien."""
    try:
        return client.get(url)
    except QuotaAtteint as e:
        raise AReporter(e.message, global_=e.global_) from e
    except LimiteDebit as e:
        raise AReporter(e.message) from e
    except DefiAntiBot as e:
        etat.ctx.defi_acces = e.fournisseur
    except DomaineBloque as e:
        etat.ctx.domaine_bloque = e.message
    except AccesRefuse:
        etat.ctx.acces_refuse = True
    except RobotsInterdit:
        if principal:
            etat.ctx.robots_autorise = False
    except PageIntrouvable:
        if principal:
            etat.ctx.page_introuvable = True
    except ErreurReseau as e:
        if principal:
            etat.erreur = e.message
    except ErreurAcces as e:
        if principal:
            etat.erreur = e.message
    return None


def pre_filtrer(row: sqlite3.Row, cfg: Config, aujourd_hui: date) -> Decision | None:
    """Exclusion SANS requête réseau quand le texte de la source suffit (moins de trafic = plus poli)."""
    texte = f"{row['nom']}\n{row['texte_source'] or ''}"
    infos = extraire_infos(texte, "", aujourd_hui)
    seuil = cfg.filtres.valeur_minimale_eur
    if cfg.filtres.critere_valeur == "dotation_totale":
        # Seule une dotation totale EXPLICITE permet d'exclure sans lire la page.
        valeur = infos.dotation_totale_eur if not infos.dotation_estimee else None
    else:
        valeur = infos.gain_principal_eur
    motifs = []
    if valeur is not None and valeur < seuil:
        motifs.append(f"gain {valeur:.0f} € < seuil de {cfg.filtres.valeur_minimale_eur:.0f} € (texte de la source)")
    if infos.date_limite and infos.date_limite < aujourd_hui:
        motifs.append(f"date limite dépassée ({infos.date_limite.isoformat()}) (texte de la source)")
    if infos.signaux_arnaque:
        motifs += [f"signal d'arnaque {s}" for s in infos.signaux_arnaque]
    return Decision(Statut.EXCLU, Mode.ABSTENTION, motifs) if motifs else None


def analyser(row: sqlite3.Row, *, client: ClientPoli, depot: Depot, journal: Journal, cfg: Config,
             aujourd_hui: date) -> Decision:
    cid = row["id"]
    decision = pre_filtrer(row, cfg, aujourd_hui)
    if decision:
        depot.mettre_a_jour(cid, statut=decision.statut.value, mode_assistance=decision.mode.value,
                            motifs=decision.motifs, avertissements=[], date_analyse=maintenant())
        journal.evenement("analyse", concours_id=cid, statut=decision.statut.value, prefiltre=True)
        return decision

    etat = _Etat()
    ctx = etat.ctx
    url_part = row["url_participation"] or row["url"]
    textes = [row["nom"], row["texte_source"] or ""]
    textes_conditions: list[str] = []
    url_reglement = None

    if est_reseau_social(url_part):
        ctx.reseau_social = True
    else:
        rep = _recuperer(client, url_part, etat, principal=True)
        # Agrégateur : suivre le lien sortant vers la vraie page du concours.
        flux = cfg.collecte.flux_par_source().get(row["source"])
        if rep is not None and row["source"] != "allowlist" and not rep.est_pdf:
            lien = lien_participation(rep.texte, rep.url, flux.lien_sortant_css if flux else None)
            if lien and domaine(lien) != domaine(rep.url):
                textes.append(texte_depuis_html(rep.texte))
                url_part = lien
                if est_reseau_social(lien):
                    ctx.reseau_social = True
                    rep = None
                else:
                    rep = _recuperer(client, lien, etat, principal=True)
        if rep is not None:
            url_part = rep.url
            if rep.detection and rep.detection.type == CAPTCHA_FORMULAIRE:
                ctx.captcha_formulaire = rep.detection.fournisseur
                journal.evenement("captcha_detecte", concours_id=cid, domaine=domaine(rep.url),
                                  fournisseur=rep.detection.fournisseur, action="mode manuel imposé")
            page = texte_depuis_pdf(rep.contenu) if rep.est_pdf else texte_depuis_html(rep.texte)
            textes.append(page)
            textes_conditions.append(page)
            liens = [] if rep.est_pdf else liens_reglement(rep.texte, rep.url)
            if liens:
                ctx.reglement_trouve = True
                url_reglement = liens[0]
                rep_r = _recuperer(client, url_reglement, etat, principal=False)
                if rep_r is not None:
                    reglement = texte_depuis_pdf(rep_r.contenu) if rep_r.est_pdf else texte_depuis_html(rep_r.texte)
                    if reglement.strip():
                        ctx.reglement_lu = True
                        textes.append(reglement)
                        textes_conditions.append(reglement)
            elif rep.est_pdf:
                ctx.reglement_trouve = ctx.reglement_lu = bool(page.strip())

    if etat.erreur and not (ctx.defi_acces or ctx.acces_refuse or ctx.domaine_bloque):
        decision = Decision(Statut.ERREUR, Mode.ABSTENTION, [etat.erreur])
        depot.mettre_a_jour(cid, statut=decision.statut.value, mode_assistance=decision.mode.value,
                            motifs=decision.motifs, date_analyse=maintenant())
        journal.evenement("analyse_erreur", concours_id=cid, erreur=etat.erreur)
        return decision

    ctx.https = url_part.startswith("https://")
    ctx.domaine_automatisation_autorisee = depot.domaine_autorise(domaine(url_part))
    url_part_can = canonicaliser(url_part)
    ctx.doublon_de = depot.trouver_par_url_participation(url_part_can, exclure_id=cid)

    infos = extraire_infos("\n".join(textes), "\n".join(textes_conditions), aujourd_hui, url_reglement)
    decision = evaluer(infos, ctx, cfg.profil, cfg.filtres, aujourd_hui)
    enregistrer_decision(depot, cid, infos, ctx, decision, url_part)
    journal.evenement("analyse", concours_id=cid, domaine=domaine(url_part), statut=decision.statut.value,
                      mode=decision.mode.value, motifs=decision.motifs)
    if ctx.defi_acces or ctx.acces_refuse or ctx.domaine_bloque:
        journal.evenement("intervention_humaine_requise", concours_id=cid, domaine=domaine(url_part),
                          raison=ctx.defi_acces or ctx.domaine_bloque or "accès refusé")
    return decision


def enregistrer_decision(depot: Depot, cid: int, infos: InfosExtraites, ctx: Contexte, decision: Decision,
                         url_part: str) -> None:
    depot.mettre_a_jour(
        cid,
        url_participation=url_part,
        url_participation_canonique=canonicaliser(url_part) if not ctx.doublon_de else None,
        gain_principal_eur=infos.gain_principal_eur,
        dotation_totale_eur=infos.dotation_totale_eur,
        date_limite=infos.date_limite.isoformat() if infos.date_limite else None,
        age_minimum=infos.age_minimum,
        url_reglement=infos.url_reglement,
        conditions={"infos": infos.vers_dict(), "contexte": ctx.vers_dict()},
        statut=decision.statut.value,
        mode_assistance=decision.mode.value,
        motifs=decision.motifs,
        avertissements=decision.avertissements,
        date_analyse=maintenant(),
    )
