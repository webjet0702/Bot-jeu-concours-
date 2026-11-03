"""Chargement et validation de la configuration (TOML)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path

SEUIL_MINIMAL_EUR = 100.0
DELAI_MINIMAL_S = 5.0
CHAMPS_PROFIL = ("email", "prenom", "nom", "code_postal", "ville", "telephone", "date_naissance")


class ErreurConfig(ValueError):
    pass


@dataclass
class Profil:
    email: str = ""
    prenom: str = ""
    nom: str = ""
    code_postal: str = ""
    ville: str = ""
    telephone: str = ""
    date_naissance: str = ""
    age: int | None = None

    def champs_transmissibles(self) -> dict[str, str]:
        """Uniquement les champs explicitement renseignés (minimisation)."""
        return {c: getattr(self, c).strip() for c in CHAMPS_PROFIL if getattr(self, c).strip()}

    def age_effectif(self, aujourd_hui: date) -> int | None:
        if self.date_naissance:
            n = date.fromisoformat(self.date_naissance)
            return aujourd_hui.year - n.year - ((aujourd_hui.month, aujourd_hui.day) < (n.month, n.day))
        return self.age


@dataclass
class Filtres:
    valeur_minimale_eur: float = SEUIL_MINIMAL_EUR
    critere_valeur: str = "lot_principal"
    jours_restants_minimum: int = 0


@dataclass
class Flux:
    url: str
    nom: str
    lien_sortant_css: str | None = None


@dataclass
class Newsletter:
    active: bool = False
    serveur_imap: str = "imap.gmail.com"
    utilisateur: str = ""
    dossier: str = "Concours"
    jours: int = 7


@dataclass
class Collecte:
    fichier_allowlist: str = "allowlist.txt"
    flux: list[Flux] = field(default_factory=list)
    newsletter: Newsletter = field(default_factory=Newsletter)

    def flux_par_source(self) -> dict[str, Flux]:
        return {f"rss:{f.nom}": f for f in self.flux}


@dataclass
class Reseau:
    user_agent: str = "ConcoursAssistant/0.1 (assistant personnel; usage non commercial)"
    delai_min_par_domaine_s: float = 10.0
    requetes_max_par_execution: int = 150
    requetes_max_par_domaine: int = 15
    timeout_s: float = 20.0
    tentatives_max: int = 2
    taille_max_octets: int = 5_000_000


@dataclass
class ViePrivee:
    retention_jours: int = 180
    dossier_donnees: str = "donnees"


@dataclass
class Config:
    racine: Path
    profil: Profil = field(default_factory=Profil)
    filtres: Filtres = field(default_factory=Filtres)
    collecte: Collecte = field(default_factory=Collecte)
    reseau: Reseau = field(default_factory=Reseau)
    vie_privee: ViePrivee = field(default_factory=ViePrivee)

    @property
    def dossier_donnees(self) -> Path:
        return self.racine / self.vie_privee.dossier_donnees

    @property
    def chemin_base(self) -> Path:
        return self.dossier_donnees / "concours.sqlite3"

    @property
    def dossier_preuves(self) -> Path:
        return self.dossier_donnees / "preuves"

    @property
    def chemin_allowlist(self) -> Path:
        return self.racine / self.collecte.fichier_allowlist


def modele_config() -> str:
    return resources.files("concours_assistant").joinpath("config_modele.toml").read_text(encoding="utf-8")


def _section(cls, donnees: dict, nom: str):
    connus = set(cls.__dataclass_fields__)
    inconnus = set(donnees) - connus
    if inconnus:
        raise ErreurConfig(f"[{nom}] clés inconnues : {', '.join(sorted(inconnus))}")
    return cls(**donnees)


def valider(cfg: Config) -> None:
    f, r = cfg.filtres, cfg.reseau
    if f.valeur_minimale_eur < SEUIL_MINIMAL_EUR:
        raise ErreurConfig(f"valeur_minimale_eur ne peut pas être inférieure à {SEUIL_MINIMAL_EUR:.0f} €")
    if f.critere_valeur not in ("lot_principal", "dotation_totale"):
        raise ErreurConfig("critere_valeur doit valoir 'lot_principal' ou 'dotation_totale'")
    if r.delai_min_par_domaine_s < DELAI_MINIMAL_S:
        raise ErreurConfig(f"delai_min_par_domaine_s doit être >= {DELAI_MINIMAL_S:.0f} s")
    if not (1 <= r.requetes_max_par_execution <= 1000 and 1 <= r.requetes_max_par_domaine <= 100):
        raise ErreurConfig("quotas de requêtes hors bornes (1-1000 par exécution, 1-100 par domaine)")
    if not 0 <= r.tentatives_max <= 3:
        raise ErreurConfig("tentatives_max doit être entre 0 et 3")
    ua = r.user_agent.strip()
    if not ua or "mozilla" in ua.lower() or "chrome" in ua.lower() or "safari" in ua.lower():
        raise ErreurConfig("user_agent doit identifier l'outil honnêtement (pas d'imitation de navigateur)")
    p = cfg.profil
    if p.date_naissance:
        try:
            date.fromisoformat(p.date_naissance)
        except ValueError as e:
            raise ErreurConfig("date_naissance doit être au format AAAA-MM-JJ") from e
    if p.email and "@" not in p.email:
        raise ErreurConfig("email invalide")


def charger_config(chemin: str | Path) -> Config:
    chemin = Path(chemin)
    if not chemin.exists():
        raise ErreurConfig(f"{chemin} introuvable : lancez d'abord `concours init`")
    with chemin.open("rb") as fh:
        brut = tomllib.load(fh)

    profil_brut = dict(brut.get("profil", {}))
    if profil_brut.get("age") in (0, ""):
        profil_brut["age"] = None
    collecte_brut = dict(brut.get("collecte", {}))
    flux = [_section(Flux, dict(x), "collecte.flux") for x in collecte_brut.pop("flux", [])]
    newsletter = _section(Newsletter, dict(collecte_brut.pop("newsletter", {})), "collecte.newsletter")

    cfg = Config(
        racine=chemin.resolve().parent,
        profil=_section(Profil, profil_brut, "profil"),
        filtres=_section(Filtres, dict(brut.get("filtres", {})), "filtres"),
        collecte=_collecte(collecte_brut, flux, newsletter),
        reseau=_section(Reseau, dict(brut.get("reseau", {})), "reseau"),
        vie_privee=_section(ViePrivee, dict(brut.get("vie_privee", {})), "vie_privee"),
    )
    valider(cfg)
    return cfg


def _collecte(brut: dict, flux: list[Flux], newsletter: Newsletter) -> Collecte:
    c = _section(Collecte, brut, "collecte")
    c.flux = flux
    c.newsletter = newsletter
    return c
