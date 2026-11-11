"""Site : en LOCAL (ton PC, http://127.0.0.1) ou EN LIGNE (Vercel + Supabase, avec mot de passe).

La même application sert les deux modes. Elle ne garde aucun état en mémoire entre deux
requêtes (jeton anti-CSRF et checklist validée dans une session signée, état réseau en base),
ce qui permet de l'héberger sur des fonctions serverless.

Sécurité :
- local : écoute sur 127.0.0.1 uniquement et refuse tout autre nom d'hôte (« DNS rebinding ») ;
- en ligne : connexion obligatoire (mot de passe haché, blocage après 5 échecs), cookies sécurisés ;
- jeton anti-CSRF sur chaque formulaire et chaque appel JavaScript ;
- échappement HTML automatique (les noms de concours viennent de sites externes) ;
- aucune route ne contourne la checklist, la validation humaine ou les règles anti-robot.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, \
    send_from_directory, session, url_for

from .. import services
from ..config import Config, ErreurConfig
from ..conformite import checklist_avant_inscription
from ..db import Depot, maintenant
from ..environnement import SourceConfig, en_ligne, ouvrir_depot
from ..journal import Journal
from ..models import Mode, Statut
from ..prefill import ResultatParticipation, affichage_disponible, preremplir_et_attendre
from ..urls import domaine
from ..vie_privee import VERSION_CONSENTEMENT, exporter, purger_anciens, purger_tout, texte_consentement
from . import auth
from .toml_edit import appliquer_sections

LIBELLES_STATUT = {
    "nouveau": ("Non analysé", "neutre"),
    "a_verifier": ("À vérifier", "attention"),
    "exclu": ("Exclu", "neutre"),
    "pret": ("Prêt", "ok"),
    "intervention_humaine": ("Intervention", "danger"),
    "inscrit": ("Inscrit", "info"),
    "abandonne": ("Abandonné", "neutre"),
    "erreur": ("Erreur", "danger"),
}
LIBELLES_MODE = {"preremplissage": "Pré-remplissage", "manuel": "Manuel", "abstention": "—", None: "—"}
FILTRES = [
    ("actifs", "À traiter", [Statut.PRET.value, Statut.A_VERIFIER.value, Statut.INTERVENTION_HUMAINE.value]),
    ("pret", "Prêts", [Statut.PRET.value]),
    ("a_verifier", "À vérifier", [Statut.A_VERIFIER.value]),
    ("intervention_humaine", "Intervention", [Statut.INTERVENTION_HUMAINE.value]),
    ("inscrit", "Inscrits", [Statut.INSCRIT.value]),
    ("nouveau", "Non analysés", [Statut.NOUVEAU.value, Statut.ERREUR.value]),
    ("exclu", "Exclus", [Statut.EXCLU.value, Statut.ABANDONNE.value]),
    ("tous", "Tous", None),
]
# Accessibles sans consentement (pour le donner, ou pour exercer ses droits).
PAGES_SANS_CONSENTEMENT = {"consentement", "consentement_valider", "static", "configuration",
                           "configuration_enregistrer", "donnees", "donnees_export", "donnees_purger",
                           "donnees_effacer", "connexion", "connexion_valider", "deconnexion",
                           "mot_de_passe", "robot", "importer"}
# Accessibles sans être connecté (version en ligne). « robot » et « importer » ont leur propre clé.
PAGES_SANS_CONNEXION = {"connexion", "connexion_valider", "static", "robot", "importer"}
# Appelés par une machine (Vercel Cron, script Gmail) avec une clé dans l'en-tête Authorization, pas de jeton CSRF.
APPELS_MACHINE = {"robot", "importer"}
MAX_EMAILS_IMPORT = 20
HOTES_LOCAUX = {"127.0.0.1", "localhost"}
BUDGET_REQUETE_S = 25        # au-delà, on n'entame plus de nouveau concours (limite de durée des requêtes)
LIMITE_PAR_RECHERCHE = 20    # concours analysés au maximum par recherche
VERROU_ANALYSE_S = 90


# ---------------------------------------------------------------- filtres Jinja


def fmt_eur(v) -> str:
    return f"{v:,.0f} €".replace(",", " ") if v is not None else "?"


def date_fr(v) -> str:
    if not v:
        return "—"
    try:
        return date.fromisoformat(str(v)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(v)


def heure_locale(v) -> str:
    from datetime import datetime
    try:
        return datetime.fromisoformat(v).astimezone().strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return str(v or "—")


def lien_sur(url) -> str:
    """N'autorise que http(s) dans un href (pas de « javascript: » venu d'un site externe)."""
    url = str(url or "")
    return url if url.startswith(("https://", "http://")) else "#"


def retour_local(valeur: str | None, defaut: str) -> str:
    return valeur if valeur and valeur.startswith("/") and not valeur.startswith("//") else defaut


# ------------------------------------------------- pré-remplissage (local seulement)


class Participation:
    """Pré-remplissage dans un Chromium VISIBLE sur ton PC, piloté depuis le site. L'envoi reste humain."""

    def __init__(self, concours_id: int, url: str, valeurs: dict[str, str], cfg: Config):
        self.concours_id = concours_id
        self.url = url
        self.messages: list[str] = []
        self.erreur: str | None = None
        self.resultat: ResultatParticipation | None = None
        self._reponses: queue.Queue[str] = queue.Queue()
        self._fil = threading.Thread(target=self._executer, args=(valeurs, cfg), daemon=True)
        self._fil.start()

    def _noter(self, message: str) -> None:
        if "À VOUS DE JOUER" in message:
            m = re.search(r"pré-remplis : (.*)", message)
            self.messages.append(f"Champs pré-remplis : {m.group(1).strip() if m else 'aucun'}")
            self.messages.append("À toi de jouer dans la fenêtre Chromium.")
        else:
            self.messages.append(message)

    def _executer(self, valeurs: dict[str, str], cfg: Config) -> None:
        depot = ouvrir_depot(cfg)
        try:
            self.resultat = preremplir_et_attendre(
                self.url, valeurs, concours_id=self.concours_id, journal=Journal(depot),
                dossier_preuves=cfg.dossier_preuves, domaine_autorise=depot.domaine_autorise,
                demander=lambda _question: self._reponses.get(), notifier=self._noter)
        except Exception as e:  # navigateur non installé, page injoignable…
            self.erreur = f"{type(e).__name__}: {e}"
            Journal(depot).evenement("preremplissage_erreur", concours_id=self.concours_id, erreur=type(e).__name__)
        finally:
            depot.fermer()

    @property
    def active(self) -> bool:
        return self._fil.is_alive()

    def terminer(self, envoye: bool) -> ResultatParticipation | None:
        if self._fil.is_alive():
            self._reponses.put("")
            self._reponses.put("o" if envoye else "n")
            self._fil.join(timeout=60)
        return self.resultat


# ----------------------------------------------------------------- application


def creer_app(chemin_config: Path | None) -> Flask:
    ligne = en_ligne()
    source = SourceConfig(chemin_config)
    app = Flask(__name__)
    if ligne:  # même clé pour toutes les instances du serveur : variable d'environnement ou base
        from ..environnement import url_base
        depot = Depot(url_base())
        try:
            app.secret_key = auth.cle_session(depot)
        finally:
            depot.fermer()
    else:
        app.secret_key = os.environ.get("CONCOURS_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(MAX_CONTENT_LENGTH=1_000_000, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=ligne, PERMANENT_SESSION_LIFETIME=timedelta(days=30))
    participations: dict[int, Participation] = {}  # local uniquement (navigateur visible sur ton PC)

    app.jinja_env.filters.update(eur=fmt_eur, date_fr=date_fr, heure=heure_locale, lien_sur=lien_sur,
                                 domaine=lambda u: domaine(u or ""))

    def jeton() -> str:
        if "jeton" not in session:
            session["jeton"] = secrets.token_urlsafe(32)
        return session["jeton"]

    @app.before_request
    def avant():
        if not ligne:
            hote = (request.host or "").rsplit(":", 1)[0]
            if hote not in HOTES_LOCAUX:
                abort(403)
        attendu = jeton()
        if request.method == "POST" and request.endpoint not in APPELS_MACHINE:
            recu = request.form.get("jeton") or request.headers.get("X-Jeton") or ""
            if not secrets.compare_digest(recu, attendu):
                abort(400)
        g.cfg = source.charger()
        g.depot = ouvrir_depot(g.cfg)
        g.journal = Journal(g.depot)
        if ligne and request.endpoint not in PAGES_SANS_CONNEXION and not session.get("connecte"):
            if request.path.startswith("/api/"):
                return jsonify({"erreur": "connexion requise"}), 401
            return redirect(url_for("connexion"))
        if request.endpoint not in PAGES_SANS_CONSENTEMENT and not g.depot.consentement_actif(VERSION_CONSENTEMENT):
            if request.path.startswith("/api/"):
                return jsonify({"erreur": "consentement requis"}), 403
            return redirect(url_for("consentement"))
        return None

    @app.teardown_request
    def apres(_exc):
        depot = g.pop("depot", None)
        if depot is not None:
            depot.fermer()

    @app.context_processor
    def contexte():
        return {"jeton": jeton(), "LIBELLES_STATUT": LIBELLES_STATUT, "LIBELLES_MODE": LIBELLES_MODE,
                "en_ligne": ligne, "connecte": bool(session.get("connecte"))}

    @app.errorhandler(ErreurConfig)
    def erreur_config(e):
        return render_template("erreur.html", titre="Configuration invalide", message=str(e),
                               chemin=source.description), 200

    @app.errorhandler(404)
    def introuvable(_e):
        return render_template("erreur.html", titre="Page introuvable", message="Ce concours ou cette page "
                               "n'existe pas (ou plus)."), 404

    def concours_ou_404(cid: int):
        r = g.depot.obtenir(cid)
        if r is None:
            abort(404)
        return r

    # ------------------------------------------------------------- connexion
    @app.get("/connexion")
    def connexion():
        if not ligne or session.get("connecte"):
            return redirect(url_for("tableau"))
        return render_template("connexion.html", premiere=not auth.mot_de_passe_defini(g.depot),
                               code_requis=auth.code_installation() is not None,
                               bloque=auth.bloque_jusqu_au(g.depot))

    @app.post("/connexion")
    def connexion_valider():
        if not ligne:
            return redirect(url_for("tableau"))
        mdp = request.form.get("mot_de_passe", "")
        if not auth.mot_de_passe_defini(g.depot):
            attendu = auth.code_installation()
            code = (request.form.get("code") or "").strip()
            if not attendu or not hmac.compare_digest(code, attendu):
                g.journal.evenement("installation_refusee")
                flash("Code d'installation incorrect.", "danger")
                return redirect(url_for("connexion"))
            try:
                auth.valider_nouveau(mdp, request.form.get("confirmation", ""))
            except ValueError as e:
                flash(f"Mot de passe refusé : {e}.", "danger")
                return redirect(url_for("connexion"))
            auth.definir(g.depot, mdp)
            g.journal.evenement("mot_de_passe_cree")
        elif not auth.tenter_connexion(g.depot, mdp):
            g.journal.evenement("connexion_echouee")
            bloque = auth.bloque_jusqu_au(g.depot)
            flash("Trop d'essais : réessaie plus tard." if bloque else "Mot de passe incorrect.", "danger")
            return redirect(url_for("connexion"))
        session.clear()
        session.permanent = True
        session["connecte"] = True
        g.journal.evenement("connexion")
        return redirect(url_for("tableau"))

    @app.post("/deconnexion")
    def deconnexion():
        session.clear()
        return redirect(url_for("connexion") if ligne else url_for("tableau"))

    @app.post("/mot-de-passe")
    def mot_de_passe():
        if not ligne:
            abort(404)
        if not auth.tenter_connexion(g.depot, request.form.get("actuel", "")):
            flash("Mot de passe actuel incorrect.", "danger")
            return redirect(url_for("donnees"))
        try:
            auth.valider_nouveau(request.form.get("nouveau", ""), request.form.get("confirmation", ""))
        except ValueError as e:
            flash(f"Mot de passe refusé : {e}.", "danger")
            return redirect(url_for("donnees"))
        auth.definir(g.depot, request.form["nouveau"])
        g.journal.evenement("mot_de_passe_change")
        flash("Mot de passe changé.", "ok")
        return redirect(url_for("donnees"))

    # ------------------------------------------------------------- tableau
    @app.get("/")
    def tableau():
        cle, _libelle, statuts = next((f for f in FILTRES if f[0] == request.args.get("filtre")), FILTRES[0])
        cfg = g.cfg
        nl = cfg.collecte.newsletter
        sources = (["Google Alertes"] if nl.active else []) + ([f"{len(cfg.collecte.flux)} flux RSS"]
                                                               if cfg.collecte.flux else []) + ["tes liens"]
        if g.depot.lire_parametre(auth.DERNIER_IMPORT):
            sources.insert(0, "Google Alertes via ton script Gmail")
        return render_template(
            "tableau.html", rows=g.depot.lister(statuts), compte=g.depot.compter_par_statut(), filtre=cle,
            filtres=FILTRES, sources=", ".join(sources), auto=request.args.get("analyser") == "1",
            besoin_mdp=nl.active and not os.environ.get("CONCOURS_IMAP_MDP"),
            profil_incomplet=not cfg.profil.email or cfg.profil.age_effectif(date.today()) is None)

    def _avec_verrou(fonction):
        if not g.depot.prendre_verrou("analyse", VERROU_ANALYSE_S):
            return {"lignes": ["Une recherche est déjà en cours (ou vient de se terminer) : patiente un peu."],
                    "en_attente": services.compter_en_attente(g.depot), "occupe": True}
        lignes: list[str] = []
        try:
            fonction(lignes.append)
        except Exception as e:  # ne jamais faire tomber le site
            lignes.append(f"⚠ Erreur : {type(e).__name__}: {e}")
        finally:
            g.depot.liberer_verrou("analyse")
        return {"lignes": lignes, "en_attente": services.compter_en_attente(g.depot)}

    def _mdp_imap() -> str | None:
        return request.form.get("mdp_imap") or os.environ.get("CONCOURS_IMAP_MDP")

    @app.post("/api/collecter")
    def api_collecter():
        mdp = _mdp_imap()
        return jsonify(_avec_verrou(lambda log: services.collecter_tout(g.cfg, g.depot, g.journal, mdp, log)))

    @app.post("/api/analyser")
    def api_analyser():
        reessayer = request.form.get("reessayer") == "1"
        return jsonify(_avec_verrou(lambda log: services.analyser_tout(
            g.cfg, g.depot, g.journal, log, limite=LIMITE_PAR_RECHERCHE, reessayer=reessayer,
            budget_s=BUDGET_REQUETE_S)))

    @app.post("/chercher")  # sans JavaScript : collecte + une tranche d'analyse
    def chercher():
        mdp = _mdp_imap()

        def travail(log):
            services.collecter_tout(g.cfg, g.depot, g.journal, mdp, log)
            services.analyser_tout(g.cfg, g.depot, g.journal, log, limite=LIMITE_PAR_RECHERCHE,
                                   budget_s=BUDGET_REQUETE_S)

        resultat = _avec_verrou(travail)
        flash(" · ".join(resultat["lignes"][-3:]) or "Recherche terminée.", "info")
        return redirect(url_for("tableau"))

    @app.route("/api/robot", methods=["GET", "POST"])
    def robot():
        """Passage automatique quotidien (Vercel Cron), protégé par le secret CRON_SECRET."""
        secret = os.environ.get("CRON_SECRET") or ""
        recu = request.headers.get("Authorization", "")
        if not secret or not hmac.compare_digest(recu, f"Bearer {secret}"):
            abort(401)
        if not g.depot.consentement_actif(VERSION_CONSENTEMENT):
            return jsonify({"lignes": ["consentement absent : rien à faire"]})

        def travail(log):
            services.collecter_tout(g.cfg, g.depot, g.journal, os.environ.get("CONCOURS_IMAP_MDP"), log)
            services.analyser_tout(g.cfg, g.depot, g.journal, log, limite=LIMITE_PAR_RECHERCHE,
                                   budget_s=BUDGET_REQUETE_S + 10)

        resultat = _avec_verrou(travail)
        g.journal.evenement("robot_quotidien", lignes=len(resultat["lignes"]))
        return jsonify(resultat)

    @app.post("/api/importer")
    def importer():
        """Le script Gmail (Apps Script, dans TON compte Google) dépose ici tes Google Alertes :
        aucun mot de passe Gmail n'est stocké. La clé d'import ne permet que ce dépôt."""
        if not auth.verifier_cle_import(g.depot, request.headers.get("Authorization", "")):
            abort(401)
        if not g.depot.consentement_actif(VERSION_CONSENTEMENT):
            return jsonify({"erreur": "consentement requis"}), 403
        emails = (request.get_json(silent=True) or {}).get("emails")
        if not isinstance(emails, list) or len(emails) > MAX_EMAILS_IMPORT \
                or not all(isinstance(e, str) for e in emails):
            return jsonify({"erreur": f"format attendu : {{\"emails\": [...]}} ({MAX_EMAILS_IMPORT} au plus)"}), 400
        nouveaux, doublons, illisibles = services.importer_emails(g.depot, g.journal, emails)
        g.depot.ecrire_parametre(auth.DERNIER_IMPORT, maintenant())
        g.depot.ecrire_parametre(auth.BILAN_IMPORT, json.dumps({"emails": len(emails), "nouveaux": nouveaux}))
        return jsonify({"recus": len(emails), "nouveaux": nouveaux, "doublons": doublons, "illisibles": illisibles})

    @app.get("/aide/gmail")
    def aide_gmail():
        site = f"https://{request.host}" if ligne else request.url_root.rstrip("/")
        script = render_template("apps_script.gs", site=site, cle=auth.cle_import(g.depot))
        try:
            bilan = json.loads(g.depot.lire_parametre(auth.BILAN_IMPORT) or "null")
        except ValueError:
            bilan = None
        return render_template("aide_gmail.html", script=script, bilan=bilan,
                               dernier=g.depot.lire_parametre(auth.DERNIER_IMPORT))

    @app.post("/aide/gmail/nouvelle-cle")
    def aide_gmail_nouvelle_cle():
        auth.nouvelle_cle_import(g.depot)
        g.journal.evenement("cle_import_changee")
        flash("Nouvelle clé créée : l'ancienne ne marche plus. Recopie le script ci-dessous dans Google Apps Script.",
              "attention")
        return redirect(url_for("aide_gmail"))

    @app.post("/ajouter")
    def ajouter():
        try:
            cid = services.ajouter_lien(g.cfg, g.depot, g.journal, request.form.get("url", ""),
                                        request.form.get("nom", ""), ecrire_allowlist=not ligne)
        except ValueError as e:
            flash(f"Lien refusé : {e}.", "danger")
            return redirect(url_for("tableau"))
        if cid is None:
            flash("Ce concours est déjà dans la liste (doublon).", "attention")
            return redirect(url_for("tableau"))
        flash("Concours ajouté : analyse en cours…", "ok")
        return redirect(url_for("tableau", filtre="tous", analyser="1"))

    # ------------------------------------------------------------- concours
    @app.get("/concours/<int:cid>")
    def concours(cid: int):
        r = concours_ou_404(cid)
        url = r["url_participation"] or r["url"]
        d = domaine(url)
        cond = json.loads(r["conditions"] or "{}")
        preuve = r["preuve"] or ""
        image = None
        if preuve.endswith(".png") and Path(preuve).parent == g.cfg.dossier_preuves and Path(preuve).exists():
            image = url_for("preuve", nom=Path(preuve).name)
        return render_template("concours.html", r=r, url=url, dom=d, infos=cond.get("infos", {}),
                               motifs=json.loads(r["motifs"] or "[]"),
                               avertissements=json.loads(r["avertissements"] or "[]"),
                               bloque=g.depot.domaine_bloque(d), autorise=g.depot.domaine_autorise(d), image=image)

    @app.get("/preuves/<path:nom>")
    def preuve(nom: str):
        return send_from_directory(g.cfg.dossier_preuves, nom)

    def _nombre(cle: str, type_=float):
        brut = (request.form.get(cle) or "").strip().replace(",", ".").replace("€", "").replace(" ", "")
        return type_(float(brut)) if brut else None

    @app.post("/concours/<int:cid>/corriger")
    def corriger(cid: int):
        r = concours_ou_404(cid)
        try:
            d = services.corriger(g.cfg, g.depot, g.journal, r, valeur=_nombre("valeur"),
                                  dotation=_nombre("dotation"), date_limite=request.form.get("date_limite") or None,
                                  age_min=_nombre("age_min", int), reglement=request.form.get("reglement") or None)
        except ValueError as e:
            flash(f"Correction refusée : {e}", "danger")
            return redirect(url_for("concours", cid=cid))
        if d is None:
            flash("Aucune correction saisie.", "attention")
        else:
            flash(f"Corrigé. Nouveau statut : {LIBELLES_STATUT[d.statut.value][0]}.", "ok")
        return redirect(url_for("concours", cid=cid))

    @app.post("/concours/<int:cid>/statut")
    def statut(cid: int):
        r = concours_ou_404(cid)
        try:
            services.forcer_statut(g.depot, g.journal, r, request.form.get("nouveau", ""),
                                   request.form.get("motif", ""))
        except ValueError as e:
            flash(f"Impossible : {e}.", "danger")
        else:
            flash("Statut mis à jour.", "ok")
        return redirect(url_for("concours", cid=cid))

    @app.post("/concours/<int:cid>/reanalyser")
    def reanalyser(cid: int):
        r = concours_ou_404(cid)
        if r["statut"] == Statut.INSCRIT.value:
            flash("Tu es déjà inscrit à ce concours : pas de nouvelle analyse.", "attention")
            return redirect(url_for("concours", cid=cid))
        resultat = _avec_verrou(lambda log: services.analyser_tout(g.cfg, g.depot, g.journal, log, ids=[cid]))
        flash(" · ".join(resultat["lignes"][-2:]) or "Analyse terminée.", "info")
        return redirect(url_for("concours", cid=cid))

    # ---------------------------------------------------------- participation
    def _checklist(r):
        return checklist_avant_inscription(r, g.cfg.profil, g.cfg.filtres, g.depot, date.today(),
                                           VERSION_CONSENTEMENT)

    def _ecran() -> bool:
        return not ligne and affichage_disponible()

    def _checklists() -> dict:
        return dict(session.get("checklists", {}))

    # Enchaînement : les concours « prêts » défilent un par un. Chacun garde SA checklist et TA
    # validation ; l'outil ne fait que présenter le suivant (rien n'est envoyé à ta place).
    def _passes() -> set[int]:
        return {int(x) for x in session.get("enchainer_passes", [])}

    def _prets_restants() -> list:
        passes = _passes()
        return [r for r in g.depot.lister([Statut.PRET.value]) if r["id"] not in passes]

    def _restants() -> int | None:
        return len(_prets_restants()) if session.get("enchainer") else None

    def _arreter_enchainement() -> None:
        session.pop("enchainer", None)
        session.pop("enchainer_passes", None)

    def _suivant_ou_fin():
        restants = _prets_restants()
        if not restants:
            _arreter_enchainement()
            flash("Plus aucun concours prêt : c'est fini pour aujourd'hui !", "ok")
            return redirect(url_for("tableau"))
        return redirect(url_for("participer", cid=restants[0]["id"]))

    @app.post("/enchainer")
    def enchainer():
        session["enchainer"] = True
        session["enchainer_passes"] = []
        g.journal.evenement("enchainement_debut", prets=len(_prets_restants()))
        return _suivant_ou_fin()

    @app.post("/enchainer/passer/<int:cid>")
    def enchainer_passer(cid: int):
        validees = _checklists()
        if validees.pop(str(cid), None):
            session["checklists"] = validees
            p = participations.pop(cid, None)
            if p:
                p.terminer(False)
        if not session.get("enchainer"):
            return redirect(url_for("concours", cid=cid))
        session["enchainer_passes"] = sorted(_passes() | {cid})
        return _suivant_ou_fin()

    @app.post("/enchainer/arreter")
    def enchainer_arreter():
        _arreter_enchainement()
        return redirect(url_for("tableau"))

    @app.get("/concours/<int:cid>/participer")
    def participer(cid: int):
        r = concours_ou_404(cid)
        points = _checklist(r)
        return render_template(
            "participer.html", r=r, automatiques=[p for p in points if p.ok is not None],
            manuels=[(i, p) for i, p in enumerate(points) if p.ok is None],
            bloquants=[p for p in points if p.ok is False],
            prefill=r["mode_assistance"] == Mode.PREREMPLISSAGE.value, ecran=_ecran(), restants=_restants())

    @app.post("/concours/<int:cid>/participer")
    def participer_valider(cid: int):
        r = concours_ou_404(cid)
        points = _checklist(r)
        bloquants = [p.libelle for p in points if p.ok is False]
        if bloquants:
            g.journal.evenement("checklist_echec", concours_id=cid, points=bloquants)
            flash("Participation impossible : " + " ; ".join(bloquants), "danger")
            return redirect(url_for("participer", cid=cid))
        confirmes = set(request.form.getlist("confirme"))
        manquants = [p.libelle for i, p in enumerate(points) if p.ok is None and str(i) not in confirmes]
        if manquants:
            g.journal.evenement("checklist_refusee", concours_id=cid, points=manquants)
            flash("Coche tous les points de vérification pour continuer.", "attention")
            return redirect(url_for("participer", cid=cid))
        g.journal.evenement("checklist_validee", concours_id=cid)

        url = r["url_participation"] or r["url"]
        mode = Mode(r["mode_assistance"])
        if request.form.get("manuel") == "1":
            mode = Mode.MANUEL
        if mode == Mode.PREREMPLISSAGE and not _ecran():
            if not ligne:
                flash("Pas d'écran disponible pour un navigateur visible : passage en mode manuel.", "attention")
            mode = Mode.MANUEL  # en ligne : jamais de navigateur automatisé, tout se fait sur ton appareil
        g.journal.evenement("participation_debut", concours_id=cid, domaine=domaine(url), mode=mode.value)
        validees = _checklists()
        validees[str(cid)] = mode.value
        session["checklists"] = validees
        if mode == Mode.PREREMPLISSAGE:
            ancienne = participations.get(cid)
            if not (ancienne and ancienne.active):
                participations[cid] = Participation(cid, url, g.cfg.profil.champs_transmissibles(), g.cfg)
        return redirect(url_for("suivi", cid=cid))

    @app.get("/concours/<int:cid>/suivi")
    def suivi(cid: int):
        r = concours_ou_404(cid)
        mode = _checklists().get(str(cid))
        if mode is None:  # pas de checklist validée : on y retourne
            return redirect(url_for("participer", cid=cid))
        return render_template("suivi.html", r=r, mode=mode, url=r["url_participation"] or r["url"],
                               valeurs=g.cfg.profil.champs_transmissibles(), p=participations.get(cid),
                               restants=_restants())

    @app.get("/concours/<int:cid>/suivi/etat")
    def suivi_etat(cid: int):
        p = participations.get(cid)
        if p is None:
            return jsonify({"messages": [], "active": False, "erreur": None})
        return jsonify({"messages": p.messages[-30:], "active": p.active, "erreur": p.erreur})

    @app.post("/concours/<int:cid>/basculer-manuel")
    def basculer_manuel(cid: int):
        concours_ou_404(cid)
        validees = _checklists()
        if str(cid) in validees:
            p = participations.pop(cid, None)
            if p:
                p.terminer(False)
            validees[str(cid)] = Mode.MANUEL.value
            session["checklists"] = validees
        return redirect(url_for("suivi", cid=cid))

    @app.post("/concours/<int:cid>/terminer")
    def terminer(cid: int):
        r = concours_ou_404(cid)
        validees = _checklists()
        mode_brut = validees.pop(str(cid), None)
        session["checklists"] = validees
        if mode_brut is None:
            return redirect(url_for("participer", cid=cid))
        mode = Mode(mode_brut)
        envoye = request.form.get("envoye") == "oui"
        abandonner = request.form.get("abandonner") == "1"
        url = r["url_participation"] or r["url"]
        if mode == Mode.PREREMPLISSAGE:
            p = participations.pop(cid, None)
            res = p.terminer(envoye) if p else None
            if res is None:  # navigateur fermé en erreur : on garde la déclaration de l'humain
                res = ResultatParticipation(envoye, preuve="confirmé par l'utilisateur (navigateur indisponible)"
                                            if envoye else None)
        else:
            note = " ".join((request.form.get("note") or "").split())[:300]
            res = ResultatParticipation(envoye, preuve=f"manuel : {note or 'confirmé par l’utilisateur'}"
                                        if envoye else None)
        services.enregistrer_resultat(g.depot, g.journal, r, mode, url, res, abandonner=abandonner)
        flash("Inscription enregistrée. Bonne chance !" if envoye else "Rien n'a été enregistré comme envoyé.",
              "ok" if envoye else "attention")
        if session.get("enchainer"):
            session["enchainer_passes"] = sorted(_passes() | {cid})  # jamais deux fois le même
            return _suivant_ou_fin()
        return redirect(url_for("concours", cid=cid))

    # -------------------------------------------------------------- domaines
    @app.get("/sites")
    def domaines():
        lignes = [(d, g.depot.domaine_bloque(d["domaine"])) for d in g.depot.lister_domaines()]
        return render_template("domaines.html", lignes=lignes)

    @app.post("/sites/<action>")
    def domaine_action(action: str):
        brut = (request.form.get("domaine") or "").strip()
        nom = domaine(brut if "://" in brut else f"https://{brut}")
        retour = retour_local(request.form.get("retour"), url_for("domaines"))
        if not nom:
            flash("Domaine invalide.", "danger")
            return redirect(retour)
        if action == "autoriser":
            justification = (request.form.get("justification") or "").strip()
            if len(justification) < 15:
                flash("Justification obligatoire (au moins 15 caractères) : cite l'article des conditions "
                      "d'utilisation ou du règlement qui autorise l'aide à la saisie.", "danger")
                return redirect(retour)
            g.depot.autoriser_domaine(nom, justification)
            g.journal.evenement("domaine_autorise", domaine=nom, justification=justification)
            flash(f"Pré-remplissage autorisé sur {nom} (sur ton PC uniquement). Relance l'analyse.", "ok")
        elif action == "retirer":
            g.depot.revoquer_domaine(nom)
            g.journal.evenement("domaine_revoque", domaine=nom)
            flash(f"Autorisation retirée pour {nom}.", "ok")
        elif action == "debloquer":
            g.depot.debloquer_domaine(nom)
            g.journal.evenement("domaine_debloque", domaine=nom, par="humain")
            flash(f"{nom} débloqué. Relance l'analyse des concours concernés.", "ok")
        else:
            abort(404)
        return redirect(retour)

    # --------------------------------------------------------------- journal
    @app.get("/journal")
    def journal():
        evenements = [(e, json.loads(e["details"] or "{}")) for e in g.depot.lire_journal(200)]
        return render_template("journal.html", evenements=evenements)

    # --------------------------------------------------------- configuration
    @app.get("/profil")
    def configuration():
        return render_template("configuration.html", cfg=g.cfg, emplacement=source.description)

    @app.post("/profil")
    def configuration_enregistrer():
        f = request.form
        try:
            sections = {
                "profil": {**{c: f.get(c, "").strip() for c in
                              ("email", "prenom", "nom", "code_postal", "ville", "telephone", "date_naissance")},
                           "age": int(f.get("age") or 0),
                           "pays_residence": (f.get("pays_residence") or "FR").strip().upper()},
                "filtres": {"valeur_minimale_eur": float((f.get("valeur_minimale_eur") or "100").replace(",", ".")),
                            "critere_valeur": f.get("critere_valeur", "lot_principal"),
                            "jours_restants_minimum": int(f.get("jours_restants_minimum") or 0)},
                "collecte.newsletter": {"active": f.get("newsletter_active") == "1",
                                        "utilisateur": f.get("newsletter_utilisateur", "").strip(),
                                        "dossier": f.get("newsletter_dossier", "").strip() or "Concours"},
            }
            source.ecrire_texte(appliquer_sections(source.lire_texte(), sections))
        except (ValueError, ErreurConfig) as e:
            flash(f"Non enregistré : {e}", "danger")
            return redirect(url_for("configuration"))
        g.journal.evenement("profil_modifie")
        flash("Profil enregistré.", "ok")
        return redirect(url_for("configuration"))

    # ------------------------------------------------------- données / RGPD
    @app.get("/consentement")
    def consentement():
        return render_template("consentement.html", texte=texte_consentement(g.cfg),
                               actif=g.depot.consentement_actif(VERSION_CONSENTEMENT))

    @app.post("/consentement")
    def consentement_valider():
        if request.form.get("decision") == "accepter":
            g.depot.enregistrer_consentement(VERSION_CONSENTEMENT)
            g.journal.evenement("consentement_accorde", version=VERSION_CONSENTEMENT)
            flash("Consentement enregistré. Commence par compléter ton profil.", "ok")
            return redirect(url_for("configuration"))
        if request.form.get("decision") == "retirer":
            g.depot.retirer_consentement()
            g.journal.evenement("consentement_retire")
            flash("Consentement retiré : l'outil ne traite plus tes données.", "attention")
        return redirect(url_for("consentement"))

    @app.get("/donnees")
    def donnees():
        return render_template("donnees.html", cfg=g.cfg, actif=g.depot.consentement_actif(VERSION_CONSENTEMENT),
                               emplacement="ta base Supabase (Paris)" if ligne else str(g.cfg.dossier_donnees))

    @app.get("/donnees/export")
    def donnees_export():
        g.cfg.dossier_donnees.mkdir(parents=True, exist_ok=True)
        chemin = exporter(g.depot, g.cfg, g.cfg.dossier_donnees / "export.json")
        g.journal.evenement("export_donnees")
        return send_file(chemin, as_attachment=True, download_name="mes-donnees-concours.json")

    @app.post("/donnees/purger")
    def donnees_purger():
        compte = purger_anciens(g.depot, g.cfg)
        g.journal.evenement("purge", **compte)
        flash(f"Purge effectuée : {compte['concours']} concours, {compte['journal']} événements, "
              f"{compte['preuves']} preuves supprimés.", "ok")
        return redirect(url_for("donnees"))

    @app.post("/donnees/effacer")
    def donnees_effacer():
        if request.form.get("confirmation", "").strip().upper() != "EFFACER":
            flash("Tape EFFACER pour confirmer.", "attention")
            return redirect(url_for("donnees"))
        if any(p.active for p in participations.values()):
            flash("Attends la fin de la participation en cours.", "attention")
            return redirect(url_for("donnees"))
        if g.depot.pg:
            purger_tout(g.cfg, g.depot)
        else:
            g.pop("depot").fermer()  # le fichier SQLite doit être fermé avant suppression
            purger_tout(g.cfg)
        participations.clear()
        session.pop("checklists", None)
        flash("Toutes tes données ont été effacées.", "ok")
        return redirect(url_for("consentement"))

    return app


def port_libre(depart: int) -> int:
    for port in range(depart, depart + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError("aucun port libre trouvé")


def lancer_site(chemin_config: Path, cfg: Config, port: int = 8765, ouvrir: bool = True) -> int:
    app = creer_app(chemin_config)
    port = port_libre(port)
    url = f"http://127.0.0.1:{port}/"
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print("\n  ┌──────────────────────────────────────────────────────────┐")
    print(f"  │  Assistant concours prêt : {url:<30}│")
    print("  │  Garde cette fenêtre ouverte. Pour arrêter : Ctrl+C.      │")
    print("  └──────────────────────────────────────────────────────────┘\n")
    if ouvrir:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
    return 0
