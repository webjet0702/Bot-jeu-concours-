"""Parcours complet : article d'agrégateur -> vraie page -> règlement -> décision."""

from datetime import date

from concours_assistant.analyse import analyser
from concours_assistant.config import Flux, Reseau
from concours_assistant.http_poli import ClientPoli
from concours_assistant.models import ConcoursCandidat

from .conftest import FausseReponse, FausseSession

AUJ = date(2026, 11, 3)
ROBOTS = FausseReponse(200, "User-agent: *\nAllow: /\n", {"content-type": "text/plain"})
REGLEMENT = """<html><body>Règlement. Société Marque SAS au capital de 500 000 €, RCS Lyon 123 456 789.
Jeu gratuit sans obligation d'achat du 1er novembre 2026 au 15 décembre 2026.
Ouvert aux personnes physiques majeures résidant en France.
Dotation : une console d'une valeur de 549 € TTC.
Toute participation effectuée par un moyen automatisé (robot, script) sera considérée comme nulle.
Règlement déposé auprès de la SELARL Exemple, commissaires de justice.</body></html>"""


def _client(depot, journal, routes):
    session = FausseSession(routes)
    return ClientPoli(Reseau(), depot, journal, session=session, horloge=lambda: 0.0, dormir=lambda s: None), session


def test_parcours_agregateur(depot, journal, cfg):
    cfg.collecte.flux = [Flux(url="https://agreg.fr/feed", nom="Agreg")]
    cid = depot.ajouter_concours(ConcoursCandidat("https://agreg.fr/article-1", "Gagnez une console", "rss:Agreg",
                                                  texte="Une console à gagner"))
    client, session = _client(depot, journal, {
        "https://agreg.fr/robots.txt": ROBOTS,
        "https://agreg.fr/article-1": FausseReponse(200, '<a href="https://marque.fr/jeu">Je participe</a>'),
        "https://marque.fr/robots.txt": ROBOTS,
        "https://marque.fr/jeu": FausseReponse(200, '<form><input type=email></form><a href="/reglement">Règlement</a>'),
        "https://marque.fr/reglement": FausseReponse(200, REGLEMENT),
    })
    d = analyser(depot.obtenir(cid), client=client, depot=depot, journal=journal, cfg=cfg, aujourd_hui=AUJ)
    row = depot.obtenir(cid)
    assert d.statut.value == "pret"
    assert d.mode.value == "manuel"  # le règlement interdit l'automatisation
    assert row["url_participation"] == "https://marque.fr/jeu"
    assert row["gain_principal_eur"] == 549 and row["date_limite"] == "2026-12-15" and row["age_minimum"] == 18
    assert row["url_reglement"] == "https://marque.fr/reglement"


def test_prefiltre_sans_requete_reseau(depot, journal, cfg):
    cid = depot.ajouter_concours(ConcoursCandidat("https://m.fr/jeu", "Gagnez un bon d'achat de 30 €", "allowlist"))
    client, session = _client(depot, journal, {})
    d = analyser(depot.obtenir(cid), client=client, depot=depot, journal=journal, cfg=cfg, aujourd_hui=AUJ)
    assert d.statut.value == "exclu" and session.appels == []


def test_defi_anti_bot_intervention_humaine(depot, journal, cfg):
    cid = depot.ajouter_concours(ConcoursCandidat("https://m.fr/jeu", "Grand jeu", "allowlist"))
    client, _ = _client(depot, journal, {
        "https://m.fr/robots.txt": ROBOTS,
        "https://m.fr/jeu": FausseReponse(403, "<title>Just a moment...</title>", {"server": "cloudflare"}),
    })
    d = analyser(depot.obtenir(cid), client=client, depot=depot, journal=journal, cfg=cfg, aujourd_hui=AUJ)
    assert d.statut.value == "intervention_humaine"
    assert "intervention_humaine_requise" in [e["type"] for e in depot.lire_journal(5)]


def test_doublon_inter_sources(depot, journal, cfg):
    a = depot.ajouter_concours(ConcoursCandidat("https://marque.fr/jeu", "Jeu Marque", "allowlist"))
    b = depot.ajouter_concours(ConcoursCandidat("https://agreg.fr/article-9", "Console à gagner", "rss:Agreg"))
    client, _ = _client(depot, journal, {
        "https://agreg.fr/robots.txt": ROBOTS,
        "https://agreg.fr/article-9": FausseReponse(200, '<a href="https://marque.fr/jeu">Participer</a>'),
        "https://marque.fr/robots.txt": ROBOTS,
        "https://marque.fr/jeu": FausseReponse(200, REGLEMENT),
    })
    d = analyser(depot.obtenir(b), client=client, depot=depot, journal=journal, cfg=cfg, aujourd_hui=AUJ)
    assert d.statut.value == "exclu" and f"#{a}" in d.motifs[0]
