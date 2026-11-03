from datetime import date

import pytest

from concours_assistant.extraction import (detecter_achat_obligatoire, detecter_interdiction_automatisation,
                                           detecter_signaux_arnaque, extraire_age, extraire_date_limite,
                                           extraire_valeurs, lien_participation, liens_reglement, parser_nombre)

AUJ = date(2026, 11, 3)


@pytest.mark.parametrize("brut,attendu", [
    ("1 500", 1500), ("1.500", 1500), ("15,99", 15.99), ("15.99", 15.99), ("1 500,50", 1500.5),
    ("1500.00", 1500), ("250", 250), ("1 200", 1200),
])
def test_parser_nombre(brut, attendu):
    assert parser_nombre(brut) == attendu


def test_capital_social_ignore():
    t = "SAS au capital de 1 000 000 €. Gagnez une trottinette d'une valeur de 450 € TTC."
    principal, _, _ = extraire_valeurs(t)
    assert principal == 450


def test_frais_remboursement_et_tarif_minute_ignores():
    t = "Frais de connexion remboursés sur la base de 0,10 € par minute. Appel 0,80 €/min."
    assert extraire_valeurs(t) == (None, None, False)


def test_dotation_totale_explicite_et_lot_principal():
    t = "3 x cartes cadeaux d'une valeur de 50 €. Dotation totale : 150 €."
    assert extraire_valeurs(t) == (50, 150, False)


def test_dotation_estimee_avec_quantite():
    t = "À gagner : un voyage d'une valeur de 2 000 € ; 10 lots de 20 €."
    principal, total, estimee = extraire_valeurs(t)
    assert principal == 2000 and total == 2200 and estimee


def test_prix_entre_parentheses_et_k_euros():
    assert extraire_valeurs("1er lot : un iPhone (999 €)")[0] == 999
    assert extraire_valeurs("Une cagnotte de 2 k€ à gagner")[0] == 2000


def test_annee_collee_a_un_montant():
    assert extraire_valeurs("au 30 novembre 2026 100 € à gagner en bon d'achat")[0] == 100


def test_date_limite_periode_et_tirage():
    t = "Jeu ouvert du 1er novembre 2026 au 30 novembre 2026 inclus. Tirage au sort le 5 décembre 2026."
    assert extraire_date_limite(t, AUJ) == date(2026, 11, 30)


def test_date_limite_numerique_et_sans_annee():
    assert extraire_date_limite("Participez jusqu'au 15/12/2026 à minuit", AUJ) == date(2026, 12, 15)
    assert extraire_date_limite("Jeu gratuit jusqu'au 20/12/2026.", AUJ) == date(2026, 12, 20)
    assert extraire_date_limite("Version 1.2.2026.4 jusqu'au", AUJ) is None
    assert extraire_date_limite("Date limite : 31 décembre", AUJ) == date(2026, 12, 31)
    assert extraire_date_limite("Valable jusqu’au 5 janvier", AUJ) == date(2027, 1, 5)


def test_dates_non_pertinentes_ignorees():
    assert extraire_date_limite("Réservé aux personnes nées avant le 01/01/2008.", AUJ) is None
    assert extraire_date_limite("Les lots seront expédiés avant le 20 décembre 2026.", AUJ) is None


def test_age():
    assert extraire_age("Jeu réservé aux personnes physiques majeures.") == (18, False)
    assert extraire_age("En cas de force majeure, l'organisateur...") == (None, False)
    assert extraire_age("Ouvert aux personnes âgées d'au moins 16 ans.")[0] == 16
    age, autorisation = extraire_age("Être majeur. Les mineurs peuvent participer avec l'autorisation de leurs parents.")
    assert age == 18 and autorisation


def test_signaux_arnaque():
    assert detecter_signaux_arnaque("Pour recevoir votre lot, réglez 2,99 € de frais de port par carte bancaire.")
    assert detecter_signaux_arnaque("Appelez le 08 99 12 34 56 pour valider.")
    # Pratique légale française : remboursement des frais sur demande avec RIB.
    assert not detecter_signaux_arnaque("Les frais de connexion sont remboursés sur demande accompagnée d'un RIB.")
    assert not detecter_signaux_arnaque("Aucune coordonnée bancaire ne vous sera jamais demandée.")


def test_achat_obligatoire():
    assert not detecter_achat_obligatoire("Jeu gratuit sans obligation d'achat.")
    assert detecter_achat_obligatoire("Pour participer, conservez votre ticket de caisse comme preuve d'achat.")


def test_interdiction_automatisation():
    assert detecter_interdiction_automatisation(
        "Toute participation par un moyen automatisé (robot, script) sera considérée comme nulle.")
    assert detecter_interdiction_automatisation("Le tirage au sort aura lieu le 5 décembre.") is None


def test_liens_reglement_et_participation():
    html = """<a href="/cgu">CGU</a><a href="/jeu/reglement.pdf">Règlement du jeu</a>
              <a href="https://marque.fr/jeu">Je participe</a><a href="/autre">Participer</a>"""
    assert liens_reglement(html, "https://agreg.fr/article")[0] == "https://agreg.fr/jeu/reglement.pdf"
    assert lien_participation(html, "https://agreg.fr/article") == "https://marque.fr/jeu"
    assert lien_participation('<a class="go" href="https://x.fr/j">Go</a>', "https://agreg.fr/a", "a.go") == \
        "https://x.fr/j"
