"""Accès à la base : SQLite sur ton PC, PostgreSQL (Supabase) pour la version en ligne.

Les requêtes sont écrites une seule fois (placeholders « ? ») et traduites pour PostgreSQL.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

from .models import ConcoursCandidat, Statut
from .urls import canonicaliser, empreinte

BLOCAGE_HUMAIN = "9999-12-31T00:00:00+00:00"  # bloqué jusqu'à ce que l'humain débloque
FENETRE_DOUBLON_JOURS = 60
SCHEMA_PG = "concours"
TABLES = ("concours", "domaines", "consentements", "journal", "parametres", "robots_cache")

_COLONNES_MODIFIABLES = {
    "url_participation", "url_participation_canonique", "organisateur", "gain_principal_eur",
    "dotation_totale_eur", "date_limite", "age_minimum", "url_reglement", "conditions",
    "mode_assistance", "motifs", "avertissements", "statut", "date_analyse",
    "date_inscription", "preuve",
}
_URL_PG_FONCTIONNELLE: str | None = None  # mémorisée pour ne pas réessayer les autres à chaque requête


def maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dans(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def est_url_postgres(cible: str) -> bool:
    return str(cible).startswith(("postgres://", "postgresql://"))


def _connecter_postgres(cibles: list[str]):
    """Essaie chaque URL (ex. deux hôtes de pooler possibles) et garde la première qui répond."""
    global _URL_PG_FONCTIONNELLE
    import psycopg
    from psycopg.rows import dict_row

    ordre = ([_URL_PG_FONCTIONNELLE] if _URL_PG_FONCTIONNELLE in cibles else []) + \
        [c for c in cibles if c != _URL_PG_FONCTIONNELLE]
    derniere: Exception | None = None
    for url in ordre:
        try:
            conn = psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=None,
                                   connect_timeout=10)
        except psycopg.OperationalError as e:
            derniere = e
            continue
        _URL_PG_FONCTIONNELLE = url
        return conn
    raise derniere or RuntimeError("aucune URL PostgreSQL")


class Depot:
    def __init__(self, cible: str | Path):
        cible = str(cible)
        self.pg = est_url_postgres(cible.split()[0]) if cible.strip() else False
        if self.pg:
            self.conn = _connecter_postgres(cible.split())
            # Le rôle applicatif a déjà ce search_path par défaut ; on le fixe aussi pour les connexions directes.
            self.conn.execute(f"SET search_path TO {SCHEMA_PG}")
        else:
            if cible != ":memory:":
                Path(cible).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(cible)
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(self.schema())
            self._migrer_sqlite()
            self.conn.execute("PRAGMA foreign_keys = ON")

    # ------------------------------------------------------------- utilitaires
    @staticmethod
    def schema(pg: bool = False) -> str:
        nom = "schema_pg.sql" if pg else "schema.sql"
        return resources.files("concours_assistant").joinpath(nom).read_text(encoding="utf-8")

    def initialiser_schema_pg(self) -> None:
        """Crée le schéma PostgreSQL (réservé à l'installation, avec un rôle qui en a le droit)."""
        self.conn.execute(self.schema(pg=True))
        self.conn.execute(f"SET search_path TO {SCHEMA_PG}")

    def _migrer_sqlite(self) -> None:
        colonnes = {r["name"] for r in self.conn.execute("PRAGMA table_info(domaines)")}
        if "dernier_acces" not in colonnes:
            self.conn.execute("ALTER TABLE domaines ADD COLUMN dernier_acces REAL")
            self.conn.commit()

    def _x(self, sql: str, params: tuple = ()):
        if self.pg:
            return self.conn.execute(sql.replace("?", "%s"), params)
        return self.conn.execute(sql, params)

    def _valider(self) -> None:
        if not self.pg:
            self.conn.commit()

    def fermer(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ concours
    def ajouter_concours(self, c: ConcoursCandidat) -> int | None:
        """Insère un concours. Renvoie None si c'est un doublon (3 clés de déduplication)."""
        url_can = canonicaliser(c.url)
        emp = empreinte(c.url, c.nom)
        limite = (datetime.now(timezone.utc) - timedelta(days=FENETRE_DOUBLON_JOURS)).isoformat(timespec="seconds")
        doublon = self._x(
            "SELECT id FROM concours WHERE url_canonique = ? OR url_participation_canonique = ? "
            "OR (empreinte = ? AND date_decouverte >= ?) LIMIT 1",
            (url_can, url_can, emp, limite),
        ).fetchone()
        if doublon:
            return None
        now = maintenant()
        ligne = self._x(
            "INSERT INTO concours (url, url_canonique, empreinte, nom, organisateur, source, "
            "texte_source, statut, date_decouverte, maj_le) VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (c.url, url_can, emp, c.nom.strip()[:300], c.organisateur, c.source,
             (c.texte or "")[:5000], Statut.NOUVEAU.value, now, now),
        ).fetchone()
        self._valider()
        return ligne["id"]

    def mettre_a_jour(self, concours_id: int, **champs) -> None:
        inconnus = set(champs) - _COLONNES_MODIFIABLES
        if inconnus:
            raise ValueError(f"colonnes non modifiables : {inconnus}")
        valeurs = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
                   for k, v in champs.items()}
        valeurs["maj_le"] = maintenant()
        assignations = ", ".join(f"{k} = ?" for k in valeurs)  # noms de colonnes : liste blanche ci-dessus
        self._x(f"UPDATE concours SET {assignations} WHERE id = ?", (*valeurs.values(), concours_id))
        self._valider()

    def obtenir(self, concours_id: int):
        return self._x("SELECT * FROM concours WHERE id = ?", (concours_id,)).fetchone()

    def lister(self, statuts: list[str] | None = None) -> list:
        sql = "SELECT * FROM concours"
        params: tuple = ()
        if statuts:
            sql += f" WHERE statut IN ({','.join('?' * len(statuts))})"
            params = tuple(statuts)
        sql += (" ORDER BY gain_principal_eur IS NULL, gain_principal_eur DESC,"
                " date_limite IS NULL, date_limite ASC, id ASC")
        return self._x(sql, params).fetchall()

    def compter_par_statut(self) -> dict[str, int]:
        return {r["statut"]: r["n"] for r in
                self._x("SELECT statut, COUNT(*) AS n FROM concours GROUP BY statut").fetchall()}

    def trouver_par_url_participation(self, url_can: str, exclure_id: int) -> int | None:
        row = self._x(
            "SELECT id FROM concours WHERE (url_participation_canonique = ? OR url_canonique = ?) AND id != ? "
            "ORDER BY id LIMIT 1", (url_can, url_can, exclure_id)).fetchone()
        return row["id"] if row else None

    # ------------------------------------------------------------------ domaines
    def _assurer_domaine(self, domaine: str) -> None:
        self._x("INSERT INTO domaines (domaine) VALUES (?) ON CONFLICT (domaine) DO NOTHING", (domaine,))

    def autoriser_domaine(self, domaine: str, justification: str) -> None:
        self._assurer_domaine(domaine)
        self._x("UPDATE domaines SET automatisation_autorisee = 1, justification = ?, verifie_le = ? "
                "WHERE domaine = ?", (justification, maintenant(), domaine))
        self._valider()

    def revoquer_domaine(self, domaine: str) -> None:
        self._assurer_domaine(domaine)
        self._x("UPDATE domaines SET automatisation_autorisee = 0, verifie_le = ? WHERE domaine = ?",
                (maintenant(), domaine))
        self._valider()

    def domaine_autorise(self, domaine: str) -> bool:
        row = self._x("SELECT automatisation_autorisee FROM domaines WHERE domaine = ?", (domaine,)).fetchone()
        return bool(row and row["automatisation_autorisee"])

    def bloquer_domaine(self, domaine: str, jusqu_au: str, raison: str) -> None:
        self._assurer_domaine(domaine)
        self._x("UPDATE domaines SET bloque_jusqu_au = ?, raison_blocage = ? WHERE domaine = ?",
                (jusqu_au, raison, domaine))
        self._valider()

    def debloquer_domaine(self, domaine: str) -> None:
        self._x("UPDATE domaines SET bloque_jusqu_au = NULL, raison_blocage = NULL WHERE domaine = ?", (domaine,))
        self._valider()

    def domaine_bloque(self, domaine: str) -> str | None:
        """Renvoie la raison du blocage s'il est toujours actif."""
        row = self._x("SELECT bloque_jusqu_au, raison_blocage FROM domaines WHERE domaine = ?",
                      (domaine,)).fetchone()
        if row and row["bloque_jusqu_au"] and row["bloque_jusqu_au"] > maintenant():
            return row["raison_blocage"] or "bloqué"
        return None

    def lister_domaines(self) -> list:
        return self._x("SELECT * FROM domaines ORDER BY domaine").fetchall()

    # ---------------------------------------- état réseau (partagé entre requêtes)
    def dernier_acces(self, domaine: str) -> float | None:
        row = self._x("SELECT dernier_acces FROM domaines WHERE domaine = ?", (domaine,)).fetchone()
        return row["dernier_acces"] if row else None

    def noter_acces(self, domaine: str, instant: float) -> None:
        self._assurer_domaine(domaine)
        self._x("UPDATE domaines SET dernier_acces = ? WHERE domaine = ?", (instant, domaine))
        self._valider()

    def robots_en_cache(self, origine: str, age_max_s: int) -> tuple[int, str] | None:
        limite = dans(timedelta(seconds=-age_max_s))
        row = self._x("SELECT statut, contenu FROM robots_cache WHERE origine = ? AND recupere_le >= ?",
                      (origine, limite)).fetchone()
        return (row["statut"], row["contenu"] or "") if row else None

    def mettre_robots_en_cache(self, origine: str, statut: int, contenu: str) -> None:
        self._x("INSERT INTO robots_cache (origine, statut, contenu, recupere_le) VALUES (?,?,?,?) "
                "ON CONFLICT (origine) DO UPDATE SET statut = excluded.statut, contenu = excluded.contenu, "
                "recupere_le = excluded.recupere_le", (origine, statut, contenu[:500_000], maintenant()))
        self._valider()

    # ---------------------------------------------------------------- paramètres
    def lire_parametre(self, cle: str) -> str | None:
        row = self._x("SELECT valeur FROM parametres WHERE cle = ?", (cle,)).fetchone()
        return row["valeur"] if row else None

    def ecrire_parametre(self, cle: str, valeur: str) -> None:
        self._x("INSERT INTO parametres (cle, valeur, maj_le) VALUES (?,?,?) "
                "ON CONFLICT (cle) DO UPDATE SET valeur = excluded.valeur, maj_le = excluded.maj_le",
                (cle, valeur, maintenant()))
        self._valider()

    def ecrire_parametre_si_absent(self, cle: str, valeur: str) -> str:
        """Écrit seulement si la clé n'existe pas encore, puis renvoie la valeur en place (sûr entre instances)."""
        self._x("INSERT INTO parametres (cle, valeur, maj_le) VALUES (?,?,?) ON CONFLICT (cle) DO NOTHING",
                (cle, valeur, maintenant()))
        self._valider()
        return self.lire_parametre(cle)

    def prendre_verrou(self, nom: str, duree_s: int) -> bool:
        """Verrou atomique (évite deux analyses en même temps, ex. double clic ou robot + toi)."""
        maintenant_iso = maintenant()
        ligne = self._x(
            "INSERT INTO parametres (cle, valeur, maj_le) VALUES (?, ?, ?) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = excluded.valeur, maj_le = excluded.maj_le "
            "WHERE parametres.valeur < ? RETURNING cle",
            (f"verrou:{nom}", dans(timedelta(seconds=duree_s)), maintenant_iso, maintenant_iso)).fetchone()
        self._valider()
        return ligne is not None

    def liberer_verrou(self, nom: str) -> None:
        self.supprimer_parametre(f"verrou:{nom}")

    def supprimer_parametre(self, cle: str) -> None:
        self._x("DELETE FROM parametres WHERE cle = ?", (cle,))
        self._valider()

    # ------------------------------------------------------------- consentement
    def enregistrer_consentement(self, version: str) -> None:
        self._x("INSERT INTO consentements (version, accorde_le) VALUES (?, ?)", (version, maintenant()))
        self._valider()

    def retirer_consentement(self) -> None:
        self._x("UPDATE consentements SET retire_le = ? WHERE retire_le IS NULL", (maintenant(),))
        self._valider()

    def consentement_actif(self, version: str | None = None) -> bool:
        sql = "SELECT 1 AS ok FROM consentements WHERE retire_le IS NULL"
        params: tuple = ()
        if version:
            sql += " AND version = ?"
            params = (version,)
        return self._x(sql, params).fetchone() is not None

    # ------------------------------------------------------------------ journal
    def ajouter_journal(self, type_: str, concours_id: int | None, domaine: str | None, details: dict) -> None:
        self._x("INSERT INTO journal (horodatage, type, concours_id, domaine, details) VALUES (?,?,?,?,?)",
                (maintenant(), type_, concours_id, domaine, json.dumps(details, ensure_ascii=False, default=str)))
        self._valider()

    def lire_journal(self, n: int = 50) -> list:
        return self._x("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    # ------------------------------------------------------------------- purge
    def purger_avant(self, limite_iso: str) -> dict[str, int]:
        c1 = self._x("DELETE FROM concours WHERE maj_le < ?", (limite_iso,)).rowcount
        c2 = self._x("DELETE FROM journal WHERE horodatage < ?", (limite_iso,)).rowcount
        self._valider()
        return {"concours": c1, "journal": c2}

    def preuves_referencees(self) -> set[str]:
        return {r["preuve"] for r in self._x("SELECT preuve FROM concours WHERE preuve IS NOT NULL").fetchall()}

    def tout_exporter(self) -> dict:
        tables = ("concours", "domaines", "consentements", "journal")
        return {t: [dict(r) for r in self._x(f"SELECT * FROM {t}").fetchall()] for t in tables}  # noms fixes

    def tout_effacer(self, garder_parametres: tuple[str, ...] = ()) -> None:
        """Efface toutes les données (version en ligne). Garde éventuellement certains paramètres (mot de passe)."""
        for table in ("journal", "concours", "domaines", "consentements", "robots_cache"):
            self._x(f"DELETE FROM {table}")  # noms fixes
        if garder_parametres:
            self._x(f"DELETE FROM parametres WHERE cle NOT IN ({','.join('?' * len(garder_parametres))})",
                    garder_parametres)
        else:
            self._x("DELETE FROM parametres")
        self._valider()
