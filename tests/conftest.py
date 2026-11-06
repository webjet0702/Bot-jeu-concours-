from __future__ import annotations

import os

from pathlib import Path

import pytest

from concours_assistant.config import Config, Reseau
from concours_assistant.db import Depot
from concours_assistant.http_poli import ClientPoli
from concours_assistant.journal import Journal


class FausseReponse:
    def __init__(self, statut: int = 200, texte: str = "", entetes: dict | None = None, contenu: bytes | None = None,
                 url: str = ""):
        self.status_code = statut
        self.headers = {k.lower(): v for k, v in (entetes or {"content-type": "text/html; charset=utf-8"}).items()}
        self._contenu = contenu if contenu is not None else texte.encode("utf-8")
        self.url = url

    @property
    def text(self) -> str:
        return self._contenu.decode("utf-8", "replace")

    def iter_content(self, taille):
        for i in range(0, len(self._contenu), taille):
            yield self._contenu[i:i + taille]

    def close(self):
        pass


class FausseSession:
    """Session HTTP simulée : `routes` associe une URL à une FausseReponse (ou une liste, consommée dans l'ordre)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.headers: dict = {}
        self.appels: list[str] = []

    def get(self, url, **kwargs):
        self.appels.append(url)
        r = self.routes.get(url)
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        if r is None:
            return FausseReponse(404, "introuvable", url=url)
        r.url = url
        return r


class Horloge:
    def __init__(self):
        self.t = 1000.0
        self.attentes: list[float] = []

    def __call__(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.attentes.append(s)
        self.t += s


URL_PG_TEST = os.environ.get("CONCOURS_TEST_PG_URL")  # ex. postgresql://testeur:testeur@127.0.0.1/concours_test
MOTEURS = ["sqlite"] + (["postgres"] if URL_PG_TEST else [])


def depot_postgres_vierge() -> Depot:
    """Schéma « concours » recréé à neuf (base de TEST uniquement)."""
    d = Depot(URL_PG_TEST)
    d.conn.execute("DROP SCHEMA IF EXISTS concours CASCADE")
    d.initialiser_schema_pg()
    return d


@pytest.fixture(params=MOTEURS)
def depot(request):
    d = depot_postgres_vierge() if request.param == "postgres" else Depot(":memory:")
    yield d
    d.fermer()


@pytest.fixture
def journal(depot):
    return Journal(depot)


@pytest.fixture
def horloge():
    return Horloge()


@pytest.fixture
def fabrique_client(depot, journal, horloge):
    def fabriquer(routes: dict, **reseau) -> tuple[ClientPoli, FausseSession]:
        session = FausseSession(routes)
        client = ClientPoli(Reseau(**reseau), depot, journal, session=session, horloge=horloge,
                            dormir=horloge.dormir)
        return client, session
    return fabriquer


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(racine=tmp_path)
    c.profil.email = "test@example.org"
    c.profil.age = 20
    return c
