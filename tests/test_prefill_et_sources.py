from email.message import EmailMessage

from concours_assistant.prefill import associer_champs, formater, participation_manuelle
from concours_assistant.sources.allowlist import lire_allowlist
from concours_assistant.sources.newsletter import liens_depuis_email


def champ(index, **kw):
    return {"index": index, "type": "text", "visible": True, "desactive": False, "rempli": False} | kw


VALEURS = {"email": "a@b.fr", "prenom": "Léa", "nom": "Martin"}


def test_association_simple():
    descr = [champ(0, type="email", name="mail"), champ(1, name="first_name"), champ(2, label="Nom")]
    assert associer_champs(descr, VALEURS) == [(0, "email"), (1, "prenom"), (2, "nom")]


def test_nom_ne_capture_pas_prenom():
    descr = [champ(0, label="Prénom"), champ(1, id="lastName")]
    assert associer_champs(descr, VALEURS) == [(0, "prenom"), (1, "nom")]


def test_champ_ambigu_laisse_a_l_humain():
    assert associer_champs([champ(0, label="Nom et prénom")], VALEURS) == []


def test_jamais_cases_a_cocher_ni_caches_ni_boutons():
    descr = [champ(0, type="checkbox", name="email_optin"), champ(1, type="hidden", name="email"),
             champ(2, type="submit", name="email"), champ(3, type="email", visible=False),
             champ(4, type="email", rempli=True)]
    assert associer_champs(descr, VALEURS) == []


def test_minimisation_seuls_les_champs_du_profil():
    descr = [champ(0, type="email"), champ(1, type="tel", name="phone"), champ(2, autocomplete="postal-code")]
    assert associer_champs(descr, {"email": "a@b.fr"}) == [(0, "email")]


def test_confirmation_email_remplie_mais_autres_doublons_ignores():
    descr = [champ(0, type="email"), champ(1, type="email", name="email_confirm"),
             champ(2, name="prenom"), champ(3, name="prenom_bis")]
    assert associer_champs(descr, VALEURS) == [(0, "email"), (1, "email")]


def test_format_date_naissance():
    assert formater("date_naissance", "2008-05-14", "text") == "14/05/2008"
    assert formater("date_naissance", "2008-05-14", "date") == "2008-05-14"


def test_participation_manuelle_n_automatise_rien():
    ouverts, reponses = [], iter(["o", "email de confirmation reçu"])
    res = participation_manuelle("https://m.fr/jeu", {"email": "a@b.fr"}, demander=lambda q: next(reponses),
                                 ouvrir=ouverts.append)
    assert ouverts == ["https://m.fr/jeu"] and res.envoye and "confirmation" in res.preuve


def test_allowlist(tmp_path):
    f = tmp_path / "allowlist.txt"
    f.write_text("# commentaire\nhttps://m.fr/jeu | Jeu M\nftp://x\n\nhttps://n.fr/j\n", encoding="utf-8")
    c = lire_allowlist(f)
    assert [x.url for x in c] == ["https://m.fr/jeu", "https://n.fr/j"] and c[0].nom == "Jeu M"


def test_newsletter_extrait_les_liens_de_concours():
    msg = EmailMessage()
    msg["Subject"] = "Les concours de la semaine"
    msg.set_content("texte")
    msg.add_alternative("""<a href="https://m.fr/jeu-noel">Participer au grand jeu de Noël</a>
        <a href="https://m.fr/desinscription">Se désinscrire</a><a href="http://n.fr/concours">Concours</a>
        <a href="https://m.fr/boutique">Boutique</a>""", subtype="html")
    liens = liens_depuis_email(msg)
    assert [c.url for c in liens] == ["https://m.fr/jeu-noel"]
