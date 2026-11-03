from datetime import date

from concours_assistant.config import Filtres, Profil
from concours_assistant.conformite import checklist_avant_inscription, evaluer
from concours_assistant.models import ConcoursCandidat, Contexte, InfosExtraites, Mode, Statut

AUJ = date(2026, 11, 3)
FILTRES = Filtres()


def infos(**kw):
    base = dict(gain_principal_eur=500, date_limite=date(2026, 12, 1), organisateur_identifie=True)
    return InfosExtraites(**(base | kw))


def ctx(**kw):
    return Contexte(**({"reglement_trouve": True, "reglement_lu": True} | kw))


def test_concours_propre_mode_manuel_par_defaut():
    d = evaluer(infos(), ctx(), Profil(age=20), FILTRES, AUJ)
    assert d.statut == Statut.PRET and d.mode == Mode.MANUEL  # domaine non autorisé par l'humain


def test_preremplissage_seulement_si_domaine_autorise():
    d = evaluer(infos(), ctx(domaine_automatisation_autorisee=True), Profil(age=20), FILTRES, AUJ)
    assert d.statut == Statut.PRET and d.mode == Mode.PREREMPLISSAGE


def test_reglement_interdisant_l_automatisation_force_le_manuel():
    d = evaluer(infos(interdit_automatisation=True), ctx(domaine_automatisation_autorisee=True), Profil(age=20),
                FILTRES, AUJ)
    assert d.mode == Mode.MANUEL


def test_captcha_force_le_manuel():
    d = evaluer(infos(), ctx(domaine_automatisation_autorisee=True, captcha_formulaire="reCAPTCHA"), Profil(age=20),
                FILTRES, AUJ)
    assert d.mode == Mode.MANUEL


def test_gain_inferieur_au_seuil_exclu():
    d = evaluer(infos(gain_principal_eur=80), ctx(), Profil(age=20), FILTRES, AUJ)
    assert d.statut == Statut.EXCLU and d.mode == Mode.ABSTENTION


def test_gain_exactement_100_accepte():
    assert evaluer(infos(gain_principal_eur=100), ctx(), Profil(age=20), FILTRES, AUJ).statut == Statut.PRET


def test_critere_dotation_totale():
    f = Filtres(critere_valeur="dotation_totale")
    assert evaluer(infos(gain_principal_eur=50, dotation_totale_eur=150), ctx(), Profil(age=20), f, AUJ).statut \
        == Statut.PRET


def test_valeur_inconnue_a_verifier():
    assert evaluer(infos(gain_principal_eur=None), ctx(), Profil(age=20), FILTRES, AUJ).statut == Statut.A_VERIFIER


def test_date_depassee():
    assert evaluer(infos(date_limite=date(2026, 11, 1)), ctx(), Profil(age=20), FILTRES, AUJ).statut == Statut.EXCLU


def test_mineur():
    assert evaluer(infos(age_minimum=18), ctx(), Profil(age=16), FILTRES, AUJ).statut == Statut.EXCLU
    d = evaluer(infos(age_minimum=18, autorisation_parentale=True), ctx(), Profil(age=16), FILTRES, AUJ)
    assert d.statut == Statut.A_VERIFIER


def test_arnaque_et_achat():
    assert evaluer(infos(signaux_arnaque=["carte bancaire"]), ctx(), Profil(age=20), FILTRES, AUJ).statut == Statut.EXCLU
    assert evaluer(infos(achat_obligatoire=True), ctx(), Profil(age=20), FILTRES, AUJ).statut == Statut.EXCLU


def test_defi_anti_bot_intervention_humaine():
    d = evaluer(infos(), ctx(defi_acces="Cloudflare"), Profil(age=20), FILTRES, AUJ)
    assert d.statut == Statut.INTERVENTION_HUMAINE and d.mode == Mode.MANUEL


def test_reseau_social_et_robots():
    assert evaluer(infos(), ctx(reseau_social=True), Profil(age=20), FILTRES, AUJ).statut == Statut.A_VERIFIER
    assert evaluer(infos(), ctx(robots_autorise=False), Profil(age=20), FILTRES, AUJ).statut == Statut.A_VERIFIER


def test_reglement_introuvable_et_http():
    assert evaluer(infos(), ctx(reglement_trouve=False), Profil(age=20), FILTRES, AUJ).statut == Statut.A_VERIFIER
    assert evaluer(infos(), ctx(https=False), Profil(age=20), FILTRES, AUJ).statut == Statut.EXCLU


def test_checklist_bloque_si_gain_insuffisant_meme_force(depot):
    depot.enregistrer_consentement("1.0")
    cid = depot.ajouter_concours(ConcoursCandidat("https://m.fr/jeu", "Jeu", "allowlist"))
    depot.mettre_a_jour(cid, statut="pret", mode_assistance="manuel", gain_principal_eur=50,
                        url_participation="https://m.fr/jeu", date_limite="2026-12-01")
    points = checklist_avant_inscription(depot.obtenir(cid), Profil(email="a@b.fr"), FILTRES, depot, AUJ, "1.0")
    echecs = [p.libelle for p in points if p.ok is False]
    assert any("Gain" in e for e in echecs)
    assert any(p.ok is None for p in points)  # points à confirmer par l'humain


def test_checklist_ok(depot):
    depot.enregistrer_consentement("1.0")
    cid = depot.ajouter_concours(ConcoursCandidat("https://m.fr/jeu", "Jeu", "allowlist"))
    depot.mettre_a_jour(cid, statut="pret", mode_assistance="manuel", gain_principal_eur=300,
                        url_participation="https://m.fr/jeu", date_limite="2026-12-01")
    points = checklist_avant_inscription(depot.obtenir(cid), Profil(email="a@b.fr"), FILTRES, depot, AUJ, "1.0")
    assert not [p for p in points if p.ok is False]
