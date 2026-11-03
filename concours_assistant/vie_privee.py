"""RGPD : consentement, export (accès/portabilité), purge (effacement, durée de conservation)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Depot

VERSION_CONSENTEMENT = "1.0"

TEXTE_CONSENTEMENT = """\
INFORMATION ET CONSENTEMENT (version {version})

Ce que l'outil stocke, UNIQUEMENT sur votre ordinateur ({dossier}) :
  • la liste des concours trouvés, leurs conditions et leur statut ;
  • un journal d'audit (emails et téléphones y sont masqués) ;
  • les preuves d'inscription (captures d'écran de la page de confirmation).
Votre profil (config.toml) ne contient que les champs que vous avez remplis.

Quand vos données quittent votre ordinateur :
  • seulement quand VOUS cliquez sur « Valider » sur le site d'un organisateur ;
  • l'organisateur devient alors responsable de leur traitement (lisez son
    règlement/sa politique : beaucoup partagent les emails avec des partenaires).

Durée de conservation : {retention} jours, puis purge (`concours purger`).
Vos droits : export (`concours exporter`), effacement total (`concours purger --tout`),
retrait du consentement (`concours consentement --retirer`).
Si vous avez moins de 15 ans, l'accord d'un parent est nécessaire en France
pour vous inscrire à des services en ligne qui traitent vos données.
"""


def texte_consentement(cfg: Config) -> str:
    return TEXTE_CONSENTEMENT.format(version=VERSION_CONSENTEMENT, dossier=cfg.dossier_donnees,
                                     retention=cfg.vie_privee.retention_jours)


def exporter(depot: Depot, cfg: Config, chemin: Path) -> Path:
    donnees = {
        "genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profil": cfg.profil.champs_transmissibles() | {"age": cfg.profil.age},
        **depot.tout_exporter(),
        "preuves": sorted(str(p) for p in cfg.dossier_preuves.glob("*")) if cfg.dossier_preuves.exists() else [],
    }
    chemin.write_text(json.dumps(donnees, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return chemin


def purger_anciens(depot: Depot, cfg: Config) -> dict[str, int]:
    limite = datetime.now(timezone.utc) - timedelta(days=cfg.vie_privee.retention_jours)
    compte = depot.purger_avant(limite.isoformat(timespec="seconds"))
    references = depot.preuves_referencees()
    supprimees = 0
    if cfg.dossier_preuves.exists():
        for f in cfg.dossier_preuves.iterdir():
            ancien = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < limite
            if f.is_file() and (ancien or str(f) not in references):
                f.unlink()
                supprimees += 1
    compte["preuves"] = supprimees
    return compte


def purger_tout(cfg: Config) -> None:
    if cfg.dossier_donnees.exists():
        shutil.rmtree(cfg.dossier_donnees)
