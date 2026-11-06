"""Site local : sécurité, consentement, parcours d'inscription, profil."""

from __future__ import annotations

import json

import pytest

from concours_assistant.config import charger_config, modele_config
from concours_assistant.db import Depot
from concours_assistant.models import ConcoursCandidat
from concours_assistant.web import creer_app
from concours_assistant.web.app import lien_sur
from concours_assistant.web.toml_edit import remplacer_section


@pytest.fixture
def site(tmp_path):
    chemin = tmp_path / "config.toml"
    chemin.write_text(modele_config().replace('email = ""', 'email = "dedie@example.org"')
                      .replace("age = 0", "age = 20"), encoding="utf-8")
    app = creer_app(chemin)
    app.config["TESTING"] = True
    cfg = charger_config(chemin)
    return app, app.test_client(), app.config["JETON"], cfg, chemin


def _depot(cfg) -> Depot:
    return Depot(cfg.chemin_base)


def _consentir(client, jeton):
    return client.post("/consentement", data={"jeton": jeton, "decision": "accepter"})


def _concours_pret(cfg, nom="Grand jeu", gain=549.0, url="https://marque.fr/jeu") -> int:
    d = _depot(cfg)
    cid = d.ajouter_concours(ConcoursCandidat(url, nom, "allowlist"))
    d.mettre_a_jour(cid, statut="pret", mode_assistance="manuel", gain_principal_eur=gain,
                    url_participation=url, date_limite="2099-12-31")
    d.fermer()
    return cid


def test_hote_etranger_refuse(site):
    _, client, *_ = site
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 403


def test_post_sans_jeton_refuse(site):
    _, client, *_ = site
    assert client.post("/consentement", data={"decision": "accepter"}).status_code == 400
    assert client.post("/consentement", data={"decision": "accepter", "jeton": "faux"}).status_code == 400


def test_consentement_obligatoire(site):
    _, client, jeton, *_ = site
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/consentement")
    assert client.get("/profil").status_code == 200  # le profil reste accessible
    _consentir(client, jeton)
    assert client.get("/").status_code == 200


def test_noms_externes_echappes(site):
    _, client, jeton, cfg, _ = site
    _consentir(client, jeton)
    _concours_pret(cfg, nom="<script>alert(1)</script>")
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_lien_javascript_neutralise():
    assert lien_sur("javascript:alert(1)") == "#"
    assert lien_sur("https://marque.fr") == "https://marque.fr"


def test_parcours_manuel_complet(site):
    _, client, jeton, cfg, _ = site
    _consentir(client, jeton)
    cid = _concours_pret(cfg)

    # Accès direct au suivi sans checklist : renvoyé vers la checklist.
    assert client.get(f"/concours/{cid}/suivi").headers["Location"].endswith(f"/concours/{cid}/participer")

    page = client.get(f"/concours/{cid}/participer").get_data(as_text=True)
    assert "Contrôles automatiques" in page

    # Checklist incomplète : refusée.
    r = client.post(f"/concours/{cid}/participer", data={"jeton": jeton, "confirme": ["0"]})
    assert r.headers["Location"].endswith(f"/concours/{cid}/participer")

    # Checklist complète : suivi en mode manuel (l'humain ouvre le lien et envoie lui-même).
    indices = [str(i) for i in range(30)]
    r = client.post(f"/concours/{cid}/participer", data={"jeton": jeton, "confirme": indices})
    assert r.headers["Location"].endswith(f"/concours/{cid}/suivi")
    suivi = client.get(f"/concours/{cid}/suivi").get_data(as_text=True)
    assert "dedie@example.org" in suivi and "https://marque.fr/jeu" in suivi

    client.post(f"/concours/{cid}/terminer", data={"jeton": jeton, "envoye": "oui", "note": "email reçu"})
    d = _depot(cfg)
    row = d.obtenir(cid)
    assert row["statut"] == "inscrit" and "email reçu" in row["preuve"]
    types = [e["type"] for e in d.lire_journal(10)]
    d.fermer()
    assert {"checklist_validee", "participation_debut", "inscription_confirmee"} <= set(types)

    # Plus de checklist validée : on ne peut pas « terminer » une seconde fois.
    r = client.post(f"/concours/{cid}/terminer", data={"jeton": jeton, "envoye": "oui"})
    assert r.headers["Location"].endswith(f"/concours/{cid}/participer")


def test_checklist_bloque_gain_insuffisant(site):
    _, client, jeton, cfg, _ = site
    _consentir(client, jeton)
    cid = _concours_pret(cfg, gain=40)
    page = client.get(f"/concours/{cid}/participer").get_data(as_text=True)
    assert "Participation impossible" in page
    r = client.post(f"/concours/{cid}/participer", data={"jeton": jeton, "confirme": [str(i) for i in range(30)]})
    assert r.headers["Location"].endswith(f"/concours/{cid}/participer")
    d = _depot(cfg)
    assert d.obtenir(cid)["statut"] == "pret"
    d.fermer()


def test_pas_de_reanalyse_si_inscrit(site):
    app, client, jeton, cfg, _ = site
    _consentir(client, jeton)
    cid = _concours_pret(cfg)
    d = _depot(cfg)
    d.mettre_a_jour(cid, statut="inscrit", date_inscription="2026-11-06T10:00:00+00:00")
    d.fermer()
    client.post(f"/concours/{cid}/reanalyser", data={"jeton": jeton})
    assert not app.extensions["taches"].etat()["en_cours"]


def test_profil_enregistre_et_valide(site):
    _, client, jeton, _, chemin = site
    base = {"jeton": jeton, "email": "nouveau@example.org", "age": "17", "pays_residence": "BE",
            "valeur_minimale_eur": "150", "critere_valeur": "lot_principal", "jours_restants_minimum": "1",
            "newsletter_utilisateur": "", "newsletter_dossier": "Concours"}
    client.post("/profil", data=base)
    cfg = charger_config(chemin)
    assert (cfg.profil.email, cfg.profil.age, cfg.profil.pays_residence) == ("nouveau@example.org", 17, "BE")
    assert cfg.filtres.valeur_minimale_eur == 150
    assert "# MINIMISATION" in chemin.read_text(encoding="utf-8")  # commentaires conservés

    avant = chemin.read_text(encoding="utf-8")
    r = client.post("/profil", data=base | {"valeur_minimale_eur": "50"})  # < 100 € : refusé
    assert r.status_code == 302 and chemin.read_text(encoding="utf-8") == avant


def test_remplacer_section_toml():
    texte = '[profil]\n# prenom = ""\nemail = ""\n\n[filtres]\nvaleur_minimale_eur = 100\n'
    sortie = remplacer_section(texte, "profil", {"prenom": 'Léa "L"', "email": "a@b.fr", "age": 18})
    assert 'prenom = "Léa \\"L\\""' in sortie and 'email = "a@b.fr"' in sortie and "age = 18" in sortie
    assert sortie.index("age = 18") < sortie.index("[filtres]")
    assert "[collecte.newsletter]\nactive = true" in remplacer_section(texte, "collecte.newsletter", {"active": True})


def test_ajout_lien_et_doublon(site):
    app, client, jeton, cfg, _ = site
    _consentir(client, jeton)
    r = client.post("/ajouter", data={"jeton": jeton, "url": "javascript:alert(1)"})
    assert r.status_code == 302
    d = _depot(cfg)
    assert d.lister() == []
    d.fermer()
    _concours_pret(cfg, url="https://marque.fr/jeu")
    client.post("/ajouter", data={"jeton": jeton, "url": "https://www.marque.fr/jeu?utm_source=x"})
    d = _depot(cfg)
    assert len(d.lister()) == 1
    d.fermer()


def test_export_et_effacement(site):
    _, client, jeton, cfg, _ = site
    _consentir(client, jeton)
    _concours_pret(cfg)
    export = client.get("/donnees/export")
    assert export.status_code == 200 and json.loads(export.data)["profil"]["email"] == "dedie@example.org"
    client.post("/donnees/effacer", data={"jeton": jeton, "confirmation": "non"})
    assert cfg.chemin_base.exists()
    client.post("/donnees/effacer", data={"jeton": jeton, "confirmation": "EFFACER"})
    assert not cfg.chemin_base.exists()
