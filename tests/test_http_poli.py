import pytest
import requests

from concours_assistant.http_poli import (AccesRefuse, DefiAntiBot, DomaineBloque, LimiteDebit, QuotaAtteint,
                                          RobotsInterdit)

from .conftest import FausseReponse

ROBOTS_OK = FausseReponse(200, "User-agent: *\nAllow: /\n", {"content-type": "text/plain"})
PAGE = FausseReponse(200, "<html><body>Concours</body></html>")


def test_robots_interdit_aucune_requete_sur_la_page(fabrique_client, journal, depot):
    client, session = fabrique_client({
        "https://a.fr/robots.txt": FausseReponse(200, "User-agent: *\nDisallow: /jeux/\n", {"content-type": "text/plain"}),
        "https://a.fr/jeux/1": PAGE,
    })
    with pytest.raises(RobotsInterdit):
        client.get("https://a.fr/jeux/1")
    assert session.appels == ["https://a.fr/robots.txt"]
    assert depot.lire_journal(1)[0]["type"] == "robots_interdit"


def test_user_agent_honnete(fabrique_client):
    client, session = fabrique_client({})
    assert session.headers["User-Agent"].startswith("ConcoursAssistant/")
    assert "Mozilla" not in session.headers["User-Agent"]


def test_delai_minimal_par_domaine(fabrique_client, horloge):
    client, _ = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK, "https://a.fr/1": PAGE, "https://a.fr/2": PAGE},
                                delai_min_par_domaine_s=10)
    client.get("https://a.fr/1")
    client.get("https://a.fr/2")
    assert horloge.attentes and all(a == pytest.approx(10) for a in horloge.attentes)


def test_crawl_delay_plus_long_respecte(fabrique_client, horloge):
    robots = FausseReponse(200, "User-agent: *\nCrawl-delay: 30\n", {"content-type": "text/plain"})
    client, _ = fabrique_client({"https://a.fr/robots.txt": robots, "https://a.fr/1": PAGE, "https://a.fr/2": PAGE})
    client.get("https://a.fr/1")
    client.get("https://a.fr/2")
    assert horloge.attentes[-1] == pytest.approx(30)


def test_defi_anti_bot_arret_et_blocage_du_domaine(fabrique_client, depot):
    challenge = FausseReponse(403, "<title>Just a moment...</title>", {"server": "cloudflare", "cf-mitigated": "challenge"})
    client, session = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK, "https://a.fr/jeu": challenge})
    with pytest.raises(DefiAntiBot):
        client.get("https://a.fr/jeu")
    assert depot.domaine_bloque("a.fr")
    nb = len(session.appels)
    with pytest.raises(DomaineBloque):  # plus aucune requête tant que l'humain n'a pas débloqué
        client.get("https://a.fr/autre")
    assert len(session.appels) == nb
    types = [e["type"] for e in depot.lire_journal(5)]
    assert "defi_anti_bot" in types


def test_429_bloque_sans_reessayer(fabrique_client, depot):
    client, session = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK,
                                       "https://a.fr/jeu": FausseReponse(429, "slow down", {"retry-after": "120"})})
    with pytest.raises(LimiteDebit):
        client.get("https://a.fr/jeu")
    assert session.appels.count("https://a.fr/jeu") == 1
    assert depot.domaine_bloque("a.fr")


def test_403_simple_demande_intervention(fabrique_client, depot):
    client, _ = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK, "https://a.fr/jeu": FausseReponse(403, "no")})
    with pytest.raises(AccesRefuse):
        client.get("https://a.fr/jeu")
    assert depot.domaine_bloque("a.fr")


def test_5xx_reessaye_avec_delai_fixe(fabrique_client, horloge):
    client, session = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK,
                                       "https://a.fr/jeu": [FausseReponse(502, "err"), PAGE]}, tentatives_max=2)
    assert client.get("https://a.fr/jeu").statut == 200
    assert session.appels.count("https://a.fr/jeu") == 2
    assert 10 in horloge.attentes  # 5 * 2**1


def test_erreur_reseau_reessayee(fabrique_client):
    client, session = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK,
                                       "https://a.fr/jeu": [requests.ConnectionError("x"), PAGE]})
    assert client.get("https://a.fr/jeu").statut == 200


def test_redirection_vers_autre_domaine_reverifie_robots(fabrique_client):
    client, session = fabrique_client({
        "https://a.fr/robots.txt": ROBOTS_OK,
        "https://a.fr/go": FausseReponse(302, "", {"location": "https://b.fr/jeu"}),
        "https://b.fr/robots.txt": FausseReponse(200, "User-agent: *\nDisallow: /\n", {"content-type": "text/plain"}),
        "https://b.fr/jeu": PAGE,
    })
    with pytest.raises(RobotsInterdit):
        client.get("https://a.fr/go")
    assert "https://b.fr/jeu" not in session.appels


def test_robots_5xx_tout_interdit(fabrique_client):
    client, _ = fabrique_client({"https://a.fr/robots.txt": FausseReponse(503, "down"), "https://a.fr/jeu": PAGE})
    with pytest.raises(RobotsInterdit):
        client.get("https://a.fr/jeu")


def test_robots_404_tout_autorise(fabrique_client):
    client, _ = fabrique_client({"https://a.fr/jeu": PAGE})
    assert client.get("https://a.fr/jeu").statut == 200


def test_quota_par_domaine(fabrique_client):
    routes = {"https://a.fr/robots.txt": ROBOTS_OK} | {f"https://a.fr/{i}": PAGE for i in range(5)}
    client, _ = fabrique_client(routes, requetes_max_par_domaine=3)
    client.get("https://a.fr/0")
    client.get("https://a.fr/1")  # robots.txt compte aussi : 3 requêtes
    with pytest.raises(QuotaAtteint) as e:
        client.get("https://a.fr/2")
    assert not e.value.global_


def test_captcha_formulaire_signale_sans_bloquer(fabrique_client, depot):
    form = FausseReponse(200, "<form><input name=email><div class='g-recaptcha'></div></form>")
    client, _ = fabrique_client({"https://a.fr/robots.txt": ROBOTS_OK, "https://a.fr/jeu": form})
    rep = client.get("https://a.fr/jeu")
    assert rep.detection and rep.detection.fournisseur == "reCAPTCHA"
    assert depot.domaine_bloque("a.fr") is None


def test_robots_en_cache_et_delai_partages_entre_requetes(depot, journal, horloge):
    """En ligne, chaque clic est une nouvelle requête : robots.txt et le délai par site sont gardés en base."""
    from concours_assistant.config import Reseau
    from concours_assistant.http_poli import ClientPoli
    from .conftest import FausseSession
    routes = {"https://a.fr/robots.txt": ROBOTS_OK, "https://a.fr/1": PAGE, "https://a.fr/2": PAGE}
    s1 = FausseSession(routes)
    ClientPoli(Reseau(), depot, journal, session=s1, horloge=horloge, dormir=horloge.dormir).get("https://a.fr/1")
    s2 = FausseSession(routes)
    ClientPoli(Reseau(), depot, journal, session=s2, horloge=horloge, dormir=horloge.dormir).get("https://a.fr/2")
    assert "https://a.fr/robots.txt" not in s2.appels  # robots.txt lu dans le cache
    assert horloge.attentes and horloge.attentes[-1] == pytest.approx(10)  # délai respecté d'un client à l'autre
