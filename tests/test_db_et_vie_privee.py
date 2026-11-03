import json
from datetime import timedelta

import pytest

from concours_assistant.config import ErreurConfig, charger_config, modele_config
from concours_assistant.db import BLOCAGE_HUMAIN, Depot, dans
from concours_assistant.journal import Journal, masquer
from concours_assistant.models import ConcoursCandidat
from concours_assistant.urls import canonicaliser
from concours_assistant.vie_privee import exporter, purger_anciens


def test_canonicalisation():
    assert canonicaliser("https://WWW.Marque.fr/jeu/?utm_source=x&b=2&a=1#haut") == "https://marque.fr/jeu?a=1&b=2"
    assert canonicaliser("https://marque.fr:443/jeu/") == "https://marque.fr/jeu"


def test_dedoublonnage(depot):
    assert depot.ajouter_concours(ConcoursCandidat("https://m.fr/jeu?utm_source=a", "Grand jeu", "rss:x"))
    assert depot.ajouter_concours(ConcoursCandidat("https://www.m.fr/jeu", "Autre titre", "allowlist")) is None
    # même domaine + même titre via une autre URL
    assert depot.ajouter_concours(ConcoursCandidat("https://m.fr/autre", "GRAND JEU !", "rss:y")) is None
    assert depot.ajouter_concours(ConcoursCandidat("https://m.fr/noel", "Jeu de Noël", "rss:y"))


def test_doublon_sur_url_de_participation(depot):
    cid = depot.ajouter_concours(ConcoursCandidat("https://agreg.fr/article-1", "Jeu A", "rss:x"))
    depot.mettre_a_jour(cid, url_participation_canonique=canonicaliser("https://marque.fr/jeu"))
    assert depot.ajouter_concours(ConcoursCandidat("https://marque.fr/jeu", "Jeu A bis", "allowlist")) is None


def test_consentement(depot):
    assert not depot.consentement_actif("1.0")
    depot.enregistrer_consentement("1.0")
    assert depot.consentement_actif("1.0")
    depot.retirer_consentement()
    assert not depot.consentement_actif("1.0")


def test_blocage_domaine(depot):
    depot.bloquer_domaine("a.fr", BLOCAGE_HUMAIN, "défi")
    assert depot.domaine_bloque("a.fr") == "défi"
    depot.debloquer_domaine("a.fr")
    assert depot.domaine_bloque("a.fr") is None
    depot.bloquer_domaine("b.fr", dans(timedelta(seconds=-1)), "expiré")
    assert depot.domaine_bloque("b.fr") is None


def test_journal_masque_les_donnees_personnelles(depot):
    Journal(depot).evenement("test", note="contact jean.dupont@mail.fr ou 06 12 34 56 78")
    details = json.loads(depot.lire_journal(1)[0]["details"])
    assert "jean.dupont" not in details["note"] and "06 12" not in details["note"]
    assert masquer({"a": ["x@y.fr"]}) == {"a": ["[email masqué]"]}


def test_purge_et_export(cfg, tmp_path):
    depot = Depot(cfg.chemin_base)
    cid = depot.ajouter_concours(ConcoursCandidat("https://m.fr/jeu", "Jeu", "allowlist"))
    depot.conn.execute("UPDATE concours SET maj_le = '2000-01-01T00:00:00+00:00' WHERE id = ?", (cid,))
    depot.conn.commit()
    cfg.dossier_preuves.mkdir(parents=True)
    (cfg.dossier_preuves / "orpheline.png").write_bytes(b"x")
    compte = purger_anciens(depot, cfg)
    assert compte["concours"] == 1 and compte["preuves"] == 1
    data = json.loads(exporter(depot, cfg, tmp_path / "export.json").read_text(encoding="utf-8"))
    assert data["profil"]["email"] == "test@example.org" and data["concours"] == []


def test_config_modele_valide_et_garde_fous(tmp_path):
    chemin = tmp_path / "config.toml"
    chemin.write_text(modele_config(), encoding="utf-8")
    cfg = charger_config(chemin)
    assert cfg.filtres.valeur_minimale_eur == 100 and cfg.profil.age is None

    chemin.write_text(modele_config().replace("valeur_minimale_eur = 100", "valeur_minimale_eur = 50"), "utf-8")
    with pytest.raises(ErreurConfig):
        charger_config(chemin)

    chemin.write_text(modele_config().replace('user_agent = "ConcoursAssistant', 'user_agent = "Mozilla/5.0 X'),
                      "utf-8")
    with pytest.raises(ErreurConfig):
        charger_config(chemin)

    chemin.write_text(modele_config().replace("delai_min_par_domaine_s = 10", "delai_min_par_domaine_s = 1"), "utf-8")
    with pytest.raises(ErreurConfig):
        charger_config(chemin)
