"""Site : sécurité, consentement, parcours d'inscription, profil ; version locale ET version en ligne."""

from __future__ import annotations

import json

import pytest

from concours_assistant.config import charger_config, modele_config
from concours_assistant.db import Depot
from concours_assistant.models import ConcoursCandidat
from concours_assistant.web import creer_app
from concours_assistant.web.app import lien_sur
from concours_assistant.web.auth import hacher, verifier
from concours_assistant.web.toml_edit import remplacer_section

from .conftest import URL_PG_TEST, depot_postgres_vierge

JETON = "jeton-de-test"
TOUT_COCHER = [str(i) for i in range(30)]


def _client(app):
    client = app.test_client()
    with client.session_transaction() as s:
        s["jeton"] = JETON
    return client


@pytest.fixture
def site(tmp_path):
    chemin = tmp_path / "config.toml"
    chemin.write_text(modele_config().replace('email = ""', 'email = "dedie@example.org"')
                      .replace("age = 0", "age = 20"), encoding="utf-8")
    app = creer_app(chemin)
    app.config["TESTING"] = True
    return app, _client(app), JETON, charger_config(chemin), chemin


def _depot(cfg) -> Depot:
    return Depot(cfg.chemin_base)


def _consentir(client, jeton=JETON):
    return client.post("/consentement", data={"jeton": jeton, "decision": "accepter"})


def _concours_pret(depot: Depot, nom="Grand jeu", gain=549.0, url="https://marque.fr/jeu", mode="manuel") -> int:
    cid = depot.ajouter_concours(ConcoursCandidat(url, nom, "allowlist"))
    depot.mettre_a_jour(cid, statut="pret", mode_assistance=mode, gain_principal_eur=gain,
                        url_participation=url, date_limite="2099-12-31")
    return cid


# ----------------------------------------------------------------- local


def test_hote_etranger_refuse(site):
    _, client, *_ = site
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 403


def test_post_sans_jeton_refuse(site):
    _, client, *_ = site
    assert client.post("/consentement", data={"decision": "accepter"}).status_code == 400
    assert client.post("/consentement", data={"decision": "accepter", "jeton": "faux"}).status_code == 400
    assert client.post("/api/analyser", headers={"X-Jeton": "faux"}).status_code == 400


def test_consentement_obligatoire(site):
    _, client, *_ = site
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/consentement")
    assert client.get("/profil").status_code == 200  # le profil reste accessible
    assert client.post("/api/analyser", headers={"X-Jeton": JETON}).status_code == 403
    _consentir(client)
    assert client.get("/").status_code == 200


def test_noms_externes_echappes(site):
    _, client, _, cfg, _ = site
    _consentir(client)
    d = _depot(cfg)
    _concours_pret(d, nom="<script>alert(1)</script>")
    d.fermer()
    html = client.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_lien_javascript_neutralise():
    assert lien_sur("javascript:alert(1)") == "#"
    assert lien_sur("https://marque.fr") == "https://marque.fr"


def test_parcours_manuel_complet(site):
    _, client, _, cfg, _ = site
    _consentir(client)
    d = _depot(cfg)
    cid = _concours_pret(d)
    d.fermer()

    # Accès direct au suivi sans checklist : renvoyé vers la checklist.
    assert client.get(f"/concours/{cid}/suivi").headers["Location"].endswith(f"/concours/{cid}/participer")
    assert "Contrôles automatiques" in client.get(f"/concours/{cid}/participer").get_data(as_text=True)

    # Checklist incomplète : refusée.
    r = client.post(f"/concours/{cid}/participer", data={"jeton": JETON, "confirme": ["0"]})
    assert r.headers["Location"].endswith(f"/concours/{cid}/participer")

    # Checklist complète : suivi en mode manuel (l'humain ouvre le lien et envoie lui-même).
    r = client.post(f"/concours/{cid}/participer", data={"jeton": JETON, "confirme": TOUT_COCHER})
    assert r.headers["Location"].endswith(f"/concours/{cid}/suivi")
    suivi = client.get(f"/concours/{cid}/suivi").get_data(as_text=True)
    assert "dedie@example.org" in suivi and "https://marque.fr/jeu" in suivi

    client.post(f"/concours/{cid}/terminer", data={"jeton": JETON, "envoye": "oui", "note": "email reçu"})
    d = _depot(cfg)
    row = d.obtenir(cid)
    types = [e["type"] for e in d.lire_journal(10)]
    d.fermer()
    assert row["statut"] == "inscrit" and "email reçu" in row["preuve"]
    assert {"checklist_validee", "participation_debut", "inscription_confirmee"} <= set(types)

    # Plus de checklist validée : on ne peut pas « terminer » une seconde fois.
    r = client.post(f"/concours/{cid}/terminer", data={"jeton": JETON, "envoye": "oui"})
    assert r.headers["Location"].endswith(f"/concours/{cid}/participer")


def test_enchainement_presente_les_prets_un_par_un(site):
    _, client, _, cfg, _ = site
    _consentir(client)
    d = _depot(cfg)
    gros = _concours_pret(d, nom="Gros lot", gain=900, url="https://marque.fr/a")
    moyen = _concours_pret(d, nom="Moyen lot", gain=300, url="https://marque.fr/b")
    petit = _concours_pret(d, nom="Petit lot", gain=150, url="https://marque.fr/c")
    d.fermer()

    assert "Enchaîner les 3 concours prêts" in client.get("/").get_data(as_text=True)
    r = client.post("/enchainer", data={"jeton": JETON})
    assert r.headers["Location"].endswith(f"/concours/{gros}/participer")  # le plus gros gain d'abord
    page = client.get(f"/concours/{gros}/participer").get_data(as_text=True)
    assert "Mode enchaînement" in page and "3 concours prêts" in page

    # Chaque concours garde sa checklist : rien ne part sans tes confirmations.
    r = client.post(f"/concours/{gros}/participer", data={"jeton": JETON, "confirme": ["0"]})
    assert r.headers["Location"].endswith(f"/concours/{gros}/participer")
    client.post(f"/concours/{gros}/participer", data={"jeton": JETON, "confirme": TOUT_COCHER})
    r = client.post(f"/concours/{gros}/terminer", data={"jeton": JETON, "envoye": "oui"})
    assert r.headers["Location"].endswith(f"/concours/{moyen}/participer")

    # « Passer » : le concours reste prêt, on passe au suivant.
    r = client.post(f"/enchainer/passer/{moyen}", data={"jeton": JETON})
    assert r.headers["Location"].endswith(f"/concours/{petit}/participer")

    # « Je n'ai pas envoyé » : pas de boucle sur le même concours ; fin de la file.
    client.post(f"/concours/{petit}/participer", data={"jeton": JETON, "confirme": TOUT_COCHER})
    r = client.post(f"/concours/{petit}/terminer", data={"jeton": JETON, "envoye": "non"})
    assert r.headers["Location"].endswith("/")
    d = _depot(cfg)
    statuts = {cid: d.obtenir(cid)["statut"] for cid in (gros, moyen, petit)}
    d.fermer()
    assert statuts == {gros: "inscrit", moyen: "pret", petit: "pret"}
    assert "Mode enchaînement" not in client.get(f"/concours/{moyen}/participer").get_data(as_text=True)


def test_checklist_bloque_gain_insuffisant(site):
    _, client, _, cfg, _ = site
    _consentir(client)
    d = _depot(cfg)
    cid = _concours_pret(d, gain=40)
    d.fermer()
    assert "Participation impossible" in client.get(f"/concours/{cid}/participer").get_data(as_text=True)
    client.post(f"/concours/{cid}/participer", data={"jeton": JETON, "confirme": TOUT_COCHER})
    assert client.get(f"/concours/{cid}/suivi").status_code == 302  # pas de checklist validée


def test_pas_de_reanalyse_si_inscrit(site):
    _, client, _, cfg, _ = site
    _consentir(client)
    d = _depot(cfg)
    cid = _concours_pret(d)
    d.mettre_a_jour(cid, statut="inscrit", date_inscription="2026-11-06T10:00:00+00:00")
    d.fermer()
    client.post(f"/concours/{cid}/reanalyser", data={"jeton": JETON})
    d = _depot(cfg)
    assert d.obtenir(cid)["statut"] == "inscrit"
    d.fermer()


def test_api_analyser_sans_rien_a_faire(site):
    _, client, *_ = site
    _consentir(client)
    r = client.post("/api/analyser", headers={"X-Jeton": JETON})
    assert r.status_code == 200 and r.get_json()["en_attente"] == 0


def test_profil_enregistre_et_valide(site):
    _, client, _, _, chemin = site
    base = {"jeton": JETON, "email": "nouveau@example.org", "age": "17", "pays_residence": "BE",
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
    _, client, _, cfg, _ = site
    _consentir(client)
    client.post("/ajouter", data={"jeton": JETON, "url": "javascript:alert(1)"})
    d = _depot(cfg)
    assert d.lister() == []
    _concours_pret(d, url="https://marque.fr/jeu")
    d.fermer()
    client.post("/ajouter", data={"jeton": JETON, "url": "https://www.marque.fr/jeu?utm_source=x"})
    d = _depot(cfg)
    assert len(d.lister()) == 1
    d.fermer()


def test_export_et_effacement(site):
    _, client, _, cfg, _ = site
    _consentir(client)
    d = _depot(cfg)
    _concours_pret(d)
    d.fermer()
    export = client.get("/donnees/export")
    assert export.status_code == 200 and json.loads(export.data)["profil"]["email"] == "dedie@example.org"
    client.post("/donnees/effacer", data={"jeton": JETON, "confirmation": "non"})
    assert cfg.chemin_base.exists()
    client.post("/donnees/effacer", data={"jeton": JETON, "confirmation": "EFFACER"})
    assert not cfg.chemin_base.exists()


def test_hachage_mot_de_passe():
    h = hacher("un mot de passe long")
    assert h.startswith("pbkdf2_sha256$") and "un mot de passe" not in h
    assert verifier("un mot de passe long", h) and not verifier("autre", h) and not verifier("x", "corrompu")


# -------------------------------------------------------------- en ligne

en_ligne_seulement = pytest.mark.skipif(not URL_PG_TEST, reason="CONCOURS_TEST_PG_URL non défini")


@pytest.fixture
def site_en_ligne(monkeypatch):
    depot_postgres_vierge().fermer()
    monkeypatch.setenv("CONCOURS_DATABASE_URL", URL_PG_TEST)
    monkeypatch.setenv("CONCOURS_SECRET_KEY", "secret-de-test")
    monkeypatch.setenv("CONCOURS_CODE_INSTALLATION", "code-installation-test")
    monkeypatch.setenv("CRON_SECRET", "cron-de-test")
    app = creer_app(None)
    app.config["TESTING"] = True
    return app, _client(app)


def _installer(client, mdp="motdepasse-solide"):
    return client.post("/connexion", data={"jeton": JETON, "code": "code-installation-test",
                                           "mot_de_passe": mdp, "confirmation": mdp})


def _reconnecter(app, mdp="motdepasse-solide"):
    client = _client(app)
    client.post("/connexion", data={"jeton": JETON, "mot_de_passe": mdp})
    with client.session_transaction() as s:
        s["jeton"] = JETON
    return client


@en_ligne_seulement
def test_en_ligne_connexion_obligatoire(site_en_ligne):
    app, client = site_en_ligne
    assert client.get("/").headers["Location"].endswith("/connexion")
    assert client.post("/api/analyser", headers={"X-Jeton": JETON}).status_code == 401
    assert client.get("/profil").headers["Location"].endswith("/connexion")
    assert "mot de passe de la base" in client.get("/connexion").get_data(as_text=True)


@en_ligne_seulement
def test_en_ligne_installation_et_connexion(site_en_ligne):
    app, client = site_en_ligne
    # Mauvais code d'installation : refusé, aucun mot de passe créé.
    client.post("/connexion", data={"jeton": JETON, "code": "faux", "mot_de_passe": "x" * 12,
                                    "confirmation": "x" * 12})
    d = Depot(URL_PG_TEST)
    assert d.lire_parametre("mot_de_passe") is None
    d.fermer()
    # Bon code : mot de passe créé et connecté.
    assert _installer(client).headers["Location"].endswith("/")
    assert client.get("/").headers["Location"].endswith("/consentement")  # connecté, consentement à donner
    with client.session_transaction() as s:
        s["jeton"] = JETON
    _consentir(client)
    assert client.get("/").status_code == 200
    # Le code d'installation ne permet plus de changer le mot de passe.
    client.post("/deconnexion", data={"jeton": JETON})
    _installer(_client(app), mdp="nouveau-mot-de-passe")
    assert _reconnecter(app, "nouveau-mot-de-passe").get("/").status_code == 302
    assert _reconnecter(app).get("/").status_code == 200


@en_ligne_seulement
def test_en_ligne_blocage_force_brute(site_en_ligne):
    app, client = site_en_ligne
    _installer(client)
    pirate = _client(app)
    for _ in range(5):
        pirate.post("/connexion", data={"jeton": JETON, "mot_de_passe": "mauvais"})
    # Même le bon mot de passe est refusé pendant le blocage.
    assert _reconnecter(app).get("/").headers["Location"].endswith("/connexion")
    assert "Trop d'essais" in _client(app).get("/connexion").get_data(as_text=True)


@en_ligne_seulement
def test_en_ligne_profil_en_base_et_pas_de_preremplissage(site_en_ligne):
    app, client = site_en_ligne
    _installer(client)
    client = _reconnecter(app)
    _consentir(client)
    client.post("/profil", data={"jeton": JETON, "email": "moi@example.org", "age": "19", "pays_residence": "FR",
                                 "valeur_minimale_eur": "100", "critere_valeur": "lot_principal",
                                 "jours_restants_minimum": "0", "newsletter_dossier": "Concours"})
    d = Depot(URL_PG_TEST)
    assert 'email = "moi@example.org"' in d.lire_parametre("config_toml")
    cid = _concours_pret(d, mode="preremplissage")
    d.autoriser_domaine("marque.fr", "test : CGU autorisant l'aide à la saisie")
    d.fermer()
    client.post(f"/concours/{cid}/participer", data={"jeton": JETON, "confirme": TOUT_COCHER})
    suivi = client.get(f"/concours/{cid}/suivi").get_data(as_text=True)
    assert "Ouvrir la page" in suivi and "Chromium" not in suivi  # en ligne : toujours manuel
    client.post(f"/concours/{cid}/terminer", data={"jeton": JETON, "envoye": "oui"})
    d = Depot(URL_PG_TEST)
    assert d.obtenir(cid)["statut"] == "inscrit"
    d.fermer()


@en_ligne_seulement
def test_en_ligne_robot_protege(site_en_ligne):
    app, client = site_en_ligne
    assert client.get("/api/robot").status_code == 401
    assert client.get("/api/robot", headers={"Authorization": "Bearer faux"}).status_code == 401
    r = client.get("/api/robot", headers={"Authorization": "Bearer cron-de-test"})
    assert r.status_code == 200 and "consentement" in r.get_json()["lignes"][0]


@en_ligne_seulement
def test_en_ligne_effacer_garde_le_mot_de_passe(site_en_ligne):
    app, client = site_en_ligne
    _installer(client)
    client = _reconnecter(app)
    _consentir(client)
    d = Depot(URL_PG_TEST)
    _concours_pret(d)
    d.fermer()
    client.post("/donnees/effacer", data={"jeton": JETON, "confirmation": "EFFACER"})
    d = Depot(URL_PG_TEST)
    assert d.lister() == [] and not d.consentement_actif() and d.lire_parametre("mot_de_passe")
    d.fermer()


@en_ligne_seulement
def test_en_ligne_un_seul_secret(monkeypatch):
    """Sans variable dédiée : code d'installation = mot de passe de la base, clé de session gardée en base."""
    depot_postgres_vierge().fermer()
    monkeypatch.setenv("CONCOURS_DATABASE_URL", URL_PG_TEST)
    for var in ("CONCOURS_SECRET_KEY", "CONCOURS_CODE_INSTALLATION", "CRON_SECRET"):
        monkeypatch.delenv(var, raising=False)
    instance_1, instance_2 = creer_app(None), creer_app(None)
    assert instance_1.secret_key == instance_2.secret_key  # une session reste valide d'un serveur à l'autre
    client = _client(instance_1)
    mdp_base = URL_PG_TEST.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    client.post("/connexion", data={"jeton": JETON, "code": "mauvais", "mot_de_passe": "x" * 12,
                                    "confirmation": "x" * 12})
    client.post("/connexion", data={"jeton": JETON, "code": mdp_base, "mot_de_passe": "mot-de-passe-site",
                                    "confirmation": "mot-de-passe-site"})
    client2 = _reconnecter(instance_2, "mot-de-passe-site")
    assert client2.get("/").headers["Location"].endswith("/consentement")  # connecté via l'autre instance
    assert _client(instance_1).get("/api/robot").status_code == 401  # robot désactivé sans CRON_SECRET
