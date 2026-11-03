"""Types partagés."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date
from enum import StrEnum


class Statut(StrEnum):
    NOUVEAU = "nouveau"                          # collecté, pas encore analysé
    A_VERIFIER = "a_verifier"                    # l'humain doit vérifier un point
    EXCLU = "exclu"                              # filtré (gain < seuil, arnaque, expiré...)
    PRET = "pret"                                # tous les contrôles automatiques passés
    INTERVENTION_HUMAINE = "intervention_humaine"  # défi anti-robot / blocage rencontré
    INSCRIT = "inscrit"                          # envoi confirmé par l'humain
    ABANDONNE = "abandonne"
    ERREUR = "erreur"


class Mode(StrEnum):
    PREREMPLISSAGE = "preremplissage"  # navigateur visible + pré-remplissage, envoi par l'humain
    MANUEL = "manuel"                  # le lien est ouvert, l'humain fait tout
    ABSTENTION = "abstention"          # l'outil ne fait rien


@dataclass
class ConcoursCandidat:
    """Concours tel que fourni par une source, avant analyse."""

    url: str
    nom: str
    source: str
    texte: str = ""
    organisateur: str | None = None


@dataclass
class InfosExtraites:
    gain_principal_eur: float | None = None
    dotation_totale_eur: float | None = None
    dotation_estimee: bool = False
    date_limite: date | None = None
    age_minimum: int | None = None
    autorisation_parentale: bool = False
    url_reglement: str | None = None
    organisateur_identifie: bool = False
    depot_commissaire: bool = False
    interdit_automatisation: bool = False
    preuve_interdiction: str | None = None
    achat_obligatoire: bool = False
    signaux_arnaque: list[str] = field(default_factory=list)

    def valeur_retenue(self, critere: str) -> float | None:
        if critere == "dotation_totale":
            if self.dotation_totale_eur is not None:
                return self.dotation_totale_eur
            return self.gain_principal_eur
        return self.gain_principal_eur

    def vers_dict(self) -> dict:
        d = asdict(self)
        d["date_limite"] = self.date_limite.isoformat() if self.date_limite else None
        return d

    @classmethod
    def depuis_dict(cls, d: dict) -> InfosExtraites:
        connus = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in connus}
        if d.get("date_limite"):
            d["date_limite"] = date.fromisoformat(d["date_limite"])
        return cls(**d)


@dataclass
class Contexte:
    """Ce que l'analyse a constaté sur le site (indépendamment du contenu)."""

    https: bool = True
    reseau_social: bool = False
    robots_autorise: bool = True
    defi_acces: str | None = None          # fournisseur du défi anti-robot détecté
    acces_refuse: bool = False             # 401/403 sans défi identifié
    domaine_bloque: str | None = None      # blocage antérieur non levé par l'humain
    page_introuvable: bool = False
    reglement_trouve: bool = False
    reglement_lu: bool = False
    captcha_formulaire: str | None = None
    domaine_automatisation_autorisee: bool = False
    doublon_de: int | None = None

    def vers_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def depuis_dict(cls, d: dict) -> Contexte:
        connus = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in connus})


@dataclass
class Decision:
    statut: Statut
    mode: Mode
    motifs: list[str] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)
