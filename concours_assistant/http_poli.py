"""Client HTTP « poli » : robots.txt, limitation de débit, arrêt sur défi anti-robot.

Garanties :
- User-Agent honnête (pas d'imitation de navigateur), aucune rotation d'IP/proxy ;
- robots.txt vérifié pour CHAQUE URL, y compris après redirection ;
- délai minimal par domaine (et Crawl-delay / Request-rate s'ils sont plus longs) ;
- quotas par exécution et par domaine ;
- 429 / 401 / 403 / défi anti-robot => arrêt immédiat, domaine bloqué, journalisation ;
- nouvelles tentatives UNIQUEMENT pour les erreurs réseau et 5xx, avec délai fixe.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .config import Reseau
from .db import BLOCAGE_HUMAIN, Depot, dans
from .detection import DEFI_ACCES, Detection, detecter_defi
from .journal import Journal
from .urls import domaine as domaine_de

MAX_REDIRECTIONS = 5
CACHE_ROBOTS_S = 24 * 3600


class ErreurAcces(Exception):
    def __init__(self, url: str, message: str):
        super().__init__(f"{message} ({url})")
        self.url = url
        self.message = message


class RobotsInterdit(ErreurAcces): ...
class AccesRefuse(ErreurAcces): ...
class LimiteDebit(ErreurAcces): ...
class DomaineBloque(ErreurAcces): ...
class PageIntrouvable(ErreurAcces): ...
class ErreurReseau(ErreurAcces): ...


class QuotaAtteint(ErreurAcces):
    def __init__(self, url: str, message: str, global_: bool):
        super().__init__(url, message)
        self.global_ = global_


class DefiAntiBot(ErreurAcces):
    def __init__(self, url: str, fournisseur: str):
        super().__init__(url, f"défi anti-robot détecté : {fournisseur}")
        self.fournisseur = fournisseur


@dataclass
class Reponse:
    url: str
    statut: int
    type_contenu: str
    contenu: bytes
    texte: str
    detection: Detection | None = None  # CAPTCHA de formulaire éventuel (pas un blocage d'accès)

    @property
    def est_pdf(self) -> bool:
        return "pdf" in self.type_contenu or self.contenu[:5] == b"%PDF-"


def _decoder(contenu: bytes, type_contenu: str) -> str:
    charset = None
    for partie in type_contenu.split(";"):
        partie = partie.strip()
        if partie.lower().startswith("charset="):
            charset = partie.split("=", 1)[1].strip("\"' ")
    if charset:
        try:
            return contenu.decode(charset, errors="replace")
        except LookupError:
            pass
    try:
        return contenu.decode("utf-8")
    except UnicodeDecodeError:
        return contenu.decode("cp1252", errors="replace")


class ClientPoli:
    def __init__(self, cfg: Reseau, depot: Depot, journal: Journal, session: requests.Session | None = None,
                 horloge: Callable[[], float] = time.time, dormir: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.depot = depot
        self.journal = journal
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent, "Accept-Language": "fr-FR,fr;q=0.9"})
        self.horloge = horloge
        self.dormir = dormir
        self._robots: dict[str, RobotFileParser] = {}
        self._dernier_acces: dict[str, float] = {}
        self._nb_total = 0
        self._nb_par_domaine: Counter[str] = Counter()

    # ------------------------------------------------------------------ public
    def get(self, url: str) -> Reponse:
        for _ in range(MAX_REDIRECTIONS + 1):
            resultat = self._get_sans_redirection(url)
            if isinstance(resultat, Reponse):
                return resultat
            url = resultat  # redirection : on revérifie robots.txt / blocage / débit pour la nouvelle URL
        raise ErreurReseau(url, "trop de redirections")

    def autorise_par_robots(self, url: str) -> bool:
        return self._robots_pour(url).can_fetch(self.cfg.user_agent, url)

    # ----------------------------------------------------------------- interne
    def _verifier_blocage(self, url: str, d: str) -> None:
        raison = self.depot.domaine_bloque(d)
        if raison:
            raise DomaineBloque(url, f"domaine bloqué : {raison}")

    def _verifier_quota(self, url: str, d: str) -> None:
        if self._nb_total >= self.cfg.requetes_max_par_execution:
            raise QuotaAtteint(url, "quota de requêtes de l'exécution atteint", global_=True)
        if self._nb_par_domaine[d] >= self.cfg.requetes_max_par_domaine:
            raise QuotaAtteint(url, f"quota de requêtes atteint pour {d}", global_=False)

    def _delai(self, rp: RobotFileParser | None) -> float:
        delai = self.cfg.delai_min_par_domaine_s
        if rp is not None:
            ua = self.cfg.user_agent
            cd = rp.crawl_delay(ua)
            if cd:
                delai = max(delai, float(cd))
            rr = rp.request_rate(ua)
            if rr and rr.requests:
                delai = max(delai, rr.seconds / rr.requests)
        return delai

    def _attendre(self, d: str, rp: RobotFileParser | None) -> None:
        # Le dernier accès est aussi lu en base : le délai est respecté même d'une requête web à l'autre.
        dernier = self._dernier_acces.get(d)
        if dernier is None:
            dernier = self.depot.dernier_acces(d)
        if dernier is not None:
            reste = self._delai(rp) - (self.horloge() - dernier)
            if reste > 0:
                self.dormir(reste)

    def _compter(self, d: str) -> None:
        instant = self.horloge()
        self._dernier_acces[d] = instant
        self.depot.noter_acces(d, instant)
        self._nb_total += 1
        self._nb_par_domaine[d] += 1

    def _lire(self, r) -> bytes:
        morceaux, total = [], 0
        for morceau in r.iter_content(65536):
            total += len(morceau)
            if total > self.cfg.taille_max_octets:
                r.close()
                raise ErreurAcces(r.url if hasattr(r, "url") else "", "réponse trop volumineuse")
            morceaux.append(morceau)
        return b"".join(morceaux)

    def _bloquer(self, d: str, jusqu_au: str, raison: str, url: str, type_evt: str) -> None:
        self.depot.bloquer_domaine(d, jusqu_au, raison)
        self.journal.evenement(type_evt, domaine=d, url=url, raison=raison,
                               action="automatisation arrêtée, intervention humaine requise")

    @staticmethod
    def _interpreter_robots(rp: RobotFileParser, statut: int, texte: str) -> None:
        if statut == 200:
            rp.parse(texte.splitlines())
        elif statut in (401, 403):
            rp.disallow_all = True  # prudence : accès à robots.txt refusé => on ne touche à rien
        elif 400 <= statut < 500:
            rp.allow_all = True     # pas de robots.txt
        else:
            rp.disallow_all = True  # 5xx / injoignable => tout interdire (RFC 9309)
        rp.modified()

    def _robots_pour(self, url: str) -> RobotFileParser:
        parties = urlsplit(url)
        origine = f"{parties.scheme}://{parties.netloc}"
        if origine in self._robots:
            return self._robots[origine]
        d = domaine_de(url)
        rp = RobotFileParser()
        rp.set_url(origine + "/robots.txt")
        en_cache = self.depot.robots_en_cache(origine, CACHE_ROBOTS_S)
        if en_cache is not None:
            self._interpreter_robots(rp, *en_cache)
            self._robots[origine] = rp
            return rp
        self._verifier_quota(url, d)
        self._attendre(d, None)
        try:
            r = self.session.get(origine + "/robots.txt", timeout=self.cfg.timeout_s, allow_redirects=True)
            texte = r.text if r.status_code == 200 else ""
            detection = detecter_defi(r.status_code, r.headers, r.text if r.status_code != 200 else "")
        except requests.RequestException as e:
            rp.disallow_all = True
            rp.modified()
            self.journal.evenement("robots_injoignable", domaine=d, erreur=type(e).__name__,
                                   decision="tout interdire (prudence)")
        else:
            if detection and detection.type == DEFI_ACCES:
                self._bloquer(d, BLOCAGE_HUMAIN, f"défi anti-robot sur robots.txt ({detection.fournisseur})",
                              origine + "/robots.txt", "defi_anti_bot")
                raise DefiAntiBot(url, detection.fournisseur)
            self._interpreter_robots(rp, r.status_code, texte)
            if r.status_code < 500:  # on ne garde pas en cache une panne passagère
                self.depot.mettre_robots_en_cache(origine, r.status_code, texte)
        finally:
            self._compter(d)
        self._robots[origine] = rp
        return rp

    def _get_sans_redirection(self, url: str) -> Reponse | str:
        if urlsplit(url).scheme not in ("http", "https"):
            raise ErreurAcces(url, "schéma d'URL non pris en charge")
        d = domaine_de(url)
        self._verifier_blocage(url, d)
        rp = self._robots_pour(url)
        if not rp.can_fetch(self.cfg.user_agent, url):
            self.journal.evenement("robots_interdit", domaine=d, url=url)
            raise RobotsInterdit(url, "robots.txt interdit cette URL")

        tentative = 0
        while True:
            self._verifier_quota(url, d)
            self._attendre(d, rp)
            try:
                r = self.session.get(url, timeout=self.cfg.timeout_s, allow_redirects=False, stream=True)
            except requests.RequestException as e:
                self._compter(d)
                if tentative < self.cfg.tentatives_max:
                    tentative += 1
                    self.journal.evenement("erreur_reseau_nouvel_essai", domaine=d, url=url,
                                           erreur=type(e).__name__, tentative=tentative)
                    self.dormir(5 * 2 ** tentative)  # délai fixe, sans aléa
                    continue
                raise ErreurReseau(url, f"erreur réseau : {type(e).__name__}") from e
            self._compter(d)

            type_contenu = r.headers.get("content-type", "") or ""
            contenu = self._lire(r)
            texte = "" if "pdf" in type_contenu else _decoder(contenu, type_contenu)
            detection = detecter_defi(r.status_code, r.headers, texte)

            if detection and detection.type == DEFI_ACCES:
                self._bloquer(d, BLOCAGE_HUMAIN, f"défi anti-robot ({detection.fournisseur})", url, "defi_anti_bot")
                raise DefiAntiBot(url, detection.fournisseur)

            statut = r.status_code
            if 300 <= statut < 400 and r.headers.get("location"):
                return urljoin(url, r.headers["location"])
            if statut == 429:
                attente = self._retry_after(r.headers.get("retry-after"))
                self._bloquer(d, dans(timedelta(seconds=max(attente, 3600))),
                              "limite de débit atteinte (HTTP 429)", url, "limite_debit")
                raise LimiteDebit(url, "le site demande de ralentir (HTTP 429)")
            if statut in (401, 403):
                self._bloquer(d, BLOCAGE_HUMAIN, f"accès refusé (HTTP {statut})", url, "acces_refuse")
                raise AccesRefuse(url, f"accès refusé (HTTP {statut})")
            if statut in (404, 410):
                raise PageIntrouvable(url, f"page introuvable (HTTP {statut})")
            if statut >= 500:
                if tentative < self.cfg.tentatives_max:
                    tentative += 1
                    self.journal.evenement("erreur_serveur_nouvel_essai", domaine=d, url=url, statut=statut,
                                           tentative=tentative)
                    self.dormir(5 * 2 ** tentative)
                    continue
                raise ErreurReseau(url, f"erreur serveur (HTTP {statut})")
            if statut >= 400:
                raise ErreurAcces(url, f"HTTP {statut}")
            return Reponse(url=url, statut=statut, type_contenu=type_contenu, contenu=contenu, texte=texte,
                           detection=detection)

    @staticmethod
    def _retry_after(valeur: str | None) -> float:
        if not valeur:
            return 0
        try:
            return float(valeur)
        except ValueError:
            try:
                from datetime import datetime, timezone
                return max(0.0, (parsedate_to_datetime(valeur) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return 0
