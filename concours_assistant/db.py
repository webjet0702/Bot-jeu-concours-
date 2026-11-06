"""Accès à la base SQLite (dépôt)."""

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

_COLONNES_MODIFIABLES = {
    "url_participation", "url_participation_canonique", "organisateur", "gain_principal_eur",
    "dotation_totale_eur", "date_limite", "age_minimum", "url_reglement", "conditions",
    "mode_assistance", "motifs", "avertissements", "statut", "date_analyse",
    "date_inscription", "preuve",
}


def maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dans(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


class Depot:
    def __init__(self, chemin: str | Path):
        chemin = str(chemin)
        if chemin != ":memory:":
            Path(chemin).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(chemin)
        self.conn.row_factory = sqlite3.Row
        schema = resources.files("concours_assistant").joinpath("schema.sql").read_text(encoding="utf-8")
        self.conn.executescript(schema)
        self.conn.execute("PRAGMA foreign_keys = ON")

    def fermer(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ concours
    def ajouter_concours(self, c: ConcoursCandidat) -> int | None:
        """Insère un concours. Renvoie None si c'est un doublon (3 clés de déduplication)."""
        url_can = canonicaliser(c.url)
        emp = empreinte(c.url, c.nom)
        limite = (datetime.now(timezone.utc) - timedelta(days=FENETRE_DOUBLON_JOURS)).isoformat(timespec="seconds")
        doublon = self.conn.execute(
            "SELECT id FROM concours WHERE url_canonique = ? OR url_participation_canonique = ? "
            "OR (empreinte = ? AND date_decouverte >= ?) LIMIT 1",
            (url_can, url_can, emp, limite),
        ).fetchone()
        if doublon:
            return None
        now = maintenant()
        cur = self.conn.execute(
            "INSERT INTO concours (url, url_canonique, empreinte, nom, organisateur, source, "
            "texte_source, statut, date_decouverte, maj_le) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (c.url, url_can, emp, c.nom.strip()[:300], c.organisateur, c.source,
             (c.texte or "")[:5000], Statut.NOUVEAU.value, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def mettre_a_jour(self, concours_id: int, **champs) -> None:
        inconnus = set(champs) - _COLONNES_MODIFIABLES
        if inconnus:
            raise ValueError(f"colonnes non modifiables : {inconnus}")
        valeurs = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
                   for k, v in champs.items()}
        valeurs["maj_le"] = maintenant()
        assignations = ", ".join(f"{k} = ?" for k in valeurs)
        self.conn.execute(f"UPDATE concours SET {assignations} WHERE id = ?", (*valeurs.values(), concours_id))
        self.conn.commit()

    def obtenir(self, concours_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM concours WHERE id = ?", (concours_id,)).fetchone()

    def lister(self, statuts: list[str] | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM concours"
        params: tuple = ()
        if statuts:
            sql += f" WHERE statut IN ({','.join('?' * len(statuts))})"
            params = tuple(statuts)
        sql += (" ORDER BY gain_principal_eur IS NULL, gain_principal_eur DESC,"
                " date_limite IS NULL, date_limite ASC, id ASC")
        return self.conn.execute(sql, params).fetchall()

    def compter_par_statut(self) -> dict[str, int]:
        return {r["statut"]: r["n"] for r in
                self.conn.execute("SELECT statut, COUNT(*) AS n FROM concours GROUP BY statut")}

    def trouver_par_url_participation(self, url_can: str, exclure_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM concours WHERE (url_participation_canonique = ? OR url_canonique = ?) AND id != ? "
            "ORDER BY id LIMIT 1", (url_can, url_can, exclure_id)).fetchone()
        return row["id"] if row else None

    # ------------------------------------------------------------------ domaines
    def _assurer_domaine(self, domaine: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO domaines (domaine) VALUES (?)", (domaine,))

    def autoriser_domaine(self, domaine: str, justification: str) -> None:
        self._assurer_domaine(domaine)
        self.conn.execute(
            "UPDATE domaines SET automatisation_autorisee = 1, justification = ?, verifie_le = ? WHERE domaine = ?",
            (justification, maintenant(), domaine))
        self.conn.commit()

    def revoquer_domaine(self, domaine: str) -> None:
        self._assurer_domaine(domaine)
        self.conn.execute("UPDATE domaines SET automatisation_autorisee = 0, verifie_le = ? WHERE domaine = ?",
                          (maintenant(), domaine))
        self.conn.commit()

    def domaine_autorise(self, domaine: str) -> bool:
        row = self.conn.execute("SELECT automatisation_autorisee FROM domaines WHERE domaine = ?",
                                (domaine,)).fetchone()
        return bool(row and row["automatisation_autorisee"])

    def bloquer_domaine(self, domaine: str, jusqu_au: str, raison: str) -> None:
        self._assurer_domaine(domaine)
        self.conn.execute("UPDATE domaines SET bloque_jusqu_au = ?, raison_blocage = ? WHERE domaine = ?",
                          (jusqu_au, raison, domaine))
        self.conn.commit()

    def debloquer_domaine(self, domaine: str) -> None:
        self.conn.execute("UPDATE domaines SET bloque_jusqu_au = NULL, raison_blocage = NULL WHERE domaine = ?",
                          (domaine,))
        self.conn.commit()

    def domaine_bloque(self, domaine: str) -> str | None:
        """Renvoie la raison du blocage s'il est toujours actif."""
        row = self.conn.execute("SELECT bloque_jusqu_au, raison_blocage FROM domaines WHERE domaine = ?",
                                (domaine,)).fetchone()
        if row and row["bloque_jusqu_au"] and row["bloque_jusqu_au"] > maintenant():
            return row["raison_blocage"] or "bloqué"
        return None

    def lister_domaines(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM domaines ORDER BY domaine").fetchall()

    # ------------------------------------------------------------- consentement
    def enregistrer_consentement(self, version: str) -> None:
        self.conn.execute("INSERT INTO consentements (version, accorde_le) VALUES (?, ?)", (version, maintenant()))
        self.conn.commit()

    def retirer_consentement(self) -> None:
        self.conn.execute("UPDATE consentements SET retire_le = ? WHERE retire_le IS NULL", (maintenant(),))
        self.conn.commit()

    def consentement_actif(self, version: str | None = None) -> bool:
        sql = "SELECT 1 FROM consentements WHERE retire_le IS NULL"
        params: tuple = ()
        if version:
            sql += " AND version = ?"
            params = (version,)
        return self.conn.execute(sql, params).fetchone() is not None

    # ------------------------------------------------------------------ journal
    def ajouter_journal(self, type_: str, concours_id: int | None, domaine: str | None, details: dict) -> None:
        self.conn.execute(
            "INSERT INTO journal (horodatage, type, concours_id, domaine, details) VALUES (?,?,?,?,?)",
            (maintenant(), type_, concours_id, domaine, json.dumps(details, ensure_ascii=False, default=str)))
        self.conn.commit()

    def lire_journal(self, n: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (n,)).fetchall()

    # ------------------------------------------------------------------- purge
    def purger_avant(self, limite_iso: str) -> dict[str, int]:
        c1 = self.conn.execute("DELETE FROM concours WHERE maj_le < ?", (limite_iso,)).rowcount
        c2 = self.conn.execute("DELETE FROM journal WHERE horodatage < ?", (limite_iso,)).rowcount
        self.conn.commit()
        return {"concours": c1, "journal": c2}

    def preuves_referencees(self) -> set[str]:
        return {r["preuve"] for r in self.conn.execute("SELECT preuve FROM concours WHERE preuve IS NOT NULL")}

    def tout_exporter(self) -> dict:
        def table(nom: str) -> list[dict]:
            return [dict(r) for r in self.conn.execute(f"SELECT * FROM {nom}")]  # noms internes fixes
        return {t: table(t) for t in ("concours", "domaines", "consentements", "journal")}
