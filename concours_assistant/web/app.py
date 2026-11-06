"""Site LOCAL (http://127.0.0.1) : la même logique que la ligne de commande, à la souris.

Sécurité :
- écoute uniquement sur 127.0.0.1, refuse tout autre nom d'hôte (protection « DNS rebinding ») ;
- jeton anti-CSRF sur chaque formulaire (un site malveillant ne peut pas déclencher d'action) ;
- échappement HTML automatique (les noms de concours viennent de sites externes) ;
- aucune route ne contourne la checklist, la validation humaine ou les règles anti-robot.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import webbrowser
from datetime import date, datetime
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, \
    send_from_directory, url_for

from .. import services
from ..config import Config, ErreurConfig, charger_config
from ..conformite import checklist_avant_inscription
from ..db import Depot
from ..journal import Journal
from ..models import Mode, Statut
from ..prefill import ResultatParticipation, affichage_disponible, preremplir_et_attendre
from ..urls import domaine
from ..vie_privee import VERSION_CONSENTEMENT, exporter, purger_anciens, purger_tout, texte_consentement
from .toml_edit import enregistrer_config

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
# Pages accessibles sans consentement (pour le donner, ou pour exercer ses droits).
PAGES_LIBRES = {"consentement", "consentement_valider", "static", "configuration", "configuration_enregistrer",
                "donnees", "donnees_export", "donnees_purger", "donnees_effacer"}
HOTES_LOCAUX = {"127.0.0.1", "localhost"}


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


# ------------------------------------------------------------- tâches de fond


class Taches:
    """Une seule tâche longue à la fois (collecte/analyse), avec son journal affiché en direct."""

    def __init__(self):
        self._verrou = threading.Lock()
        self.nom: str | None = None
        self.lignes: list[str] = []
        self.en_cours = False
        self.termine_le: str | None = None

    def lancer(self, nom: str, fonction) -> bool:
        with self._verrou:
            if self.en_cours:
                return False
            self.nom, self.lignes, self.en_cours, self.termine_le = nom, [], True, None
        threading.Thread(target=self._executer, args=(fonction,), daemon=True).start()
        return True

    def journaliser(self, ligne: str) -> None:
        with self._verrou:
            self.lignes = (self.lignes + [ligne])[-300:]

    def _executer(self, fonction) -> None:
        try:
            fonction(self.journaliser)
        except Exception as e:  # la tâche ne doit jamais faire tomber le site
            self.journaliser(f"⚠ Erreur : {type(e).__name__}: {e}")
        finally:
            with self._verrou:
                self.en_cours = False
                self.termine_le = datetime.now().strftime("%H:%M")

    def etat(self) -> dict:
        with self._verrou:
            return {"nom": self.nom, "en_cours": self.en_cours, "lignes": list(self.lignes[-80:]),
                    "termine_le": self.termine_le}


class Participation:
    """Pré-remplissage dans un Chromium VISIBLE, piloté depuis le site. L'envoi reste fait par l'humain."""

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
        depot = Depot(cfg.chemin_base)
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


def creer_app(chemin_config: Path) -> Flask:
    chemin_config = Path(chemin_config).resolve()
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config["MAX_CONTENT_LENGTH"] = 1_000_000
    jeton = secrets.token_urlsafe(32)
    app.config["JETON"] = jeton
    taches = Taches()
    participations: dict[int, Participation] = {}
    checklists_validees: dict[int, Mode] = {}

    app.jinja_env.filters.update(eur=fmt_eur, date_fr=date_fr, heure=heure_locale, lien_sur=lien_sur,
                                 domaine=lambda u: domaine(u or ""))
    app.extensions["taches"] = taches

    @app.before_request
    def avant():
        hote = (request.host or "").rsplit(":", 1)[0]
        if hote not in HOTES_LOCAUX:
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.form.get("jeton", ""), jeton):
            abort(400)
        g.cfg = charger_config(chemin_config)
        g.depot = Depot(g.cfg.chemin_base)
        g.journal = Journal(g.depot)
        if request.endpoint not in PAGES_LIBRES and not g.depot.consentement_actif(VERSION_CONSENTEMENT):
            return redirect(url_for("consentement"))
        return None

    @app.teardown_request
    def apres(_exc):
        depot = g.pop("depot", None)
        if depot is not None:
            depot.fermer()

    @app.context_processor
    def contexte():
        return {"jeton": jeton, "tache": taches.etat(), "LIBELLES_STATUT": LIBELLES_STATUT,
                "LIBELLES_MODE": LIBELLES_MODE}

    @app.errorhandler(ErreurConfig)
    def erreur_config(e):
        return render_template("erreur.html", titre="Configuration invalide", message=str(e),
                               chemin=str(chemin_config)), 200

    @app.errorhandler(404)
    def introuvable(_e):
        return render_template("erreur.html", titre="Page introuvable", message="Ce concours ou cette page "
                               "n'existe pas (ou plus)."), 404

    def concours_ou_404(cid: int):
        r = g.depot.obtenir(cid)
        if r is None:
            abort(404)
        return r

    # ------------------------------------------------------------- tableau
    @app.get("/")
    def tableau():
        cle, _libelle, statuts = next((f for f in FILTRES if f[0] == request.args.get("filtre")), FILTRES[0])
        cfg = g.cfg
        nl = cfg.collecte.newsletter
        sources = (["Google Alertes"] if nl.active else []) + ([f"{len(cfg.collecte.flux)} flux RSS"]
                                                               if cfg.collecte.flux else []) + ["tes liens"]
        return render_template(
            "tableau.html", rows=g.depot.lister(statuts), compte=g.depot.compter_par_statut(), filtre=cle,
            filtres=FILTRES, sources=", ".join(sources),
            besoin_mdp=nl.active and not os.environ.get("CONCOURS_IMAP_MDP"),
            profil_incomplet=not cfg.profil.email or cfg.profil.age_effectif(date.today()) is None)

    def lancer_tache(nom: str, travail) -> None:
        cfg = g.cfg

        def enveloppe(log):
            depot = Depot(cfg.chemin_base)
            try:
                travail(cfg, depot, Journal(depot), log)
            finally:
                depot.fermer()

        if not taches.lancer(nom, enveloppe):
            flash("Une tâche est déjà en cours : attends qu'elle se termine.", "attention")

    @app.post("/chercher")
    def chercher():
        mdp = request.form.get("mdp_imap") or os.environ.get("CONCOURS_IMAP_MDP")

        def travail(cfg, depot, journal, log):
            services.collecter_tout(cfg, depot, journal, mdp, log)
            services.analyser_tout(cfg, depot, journal, log, limite=20)

        lancer_tache("Recherche de concours", travail)
        return redirect(url_for("tableau"))

    @app.post("/analyser")
    def analyser():
        reessayer = request.form.get("reessayer") == "1"
        lancer_tache("Analyse", lambda cfg, depot, journal, log:
                     services.analyser_tout(cfg, depot, journal, log, limite=20, reessayer=reessayer))
        return redirect(url_for("tableau", filtre=request.form.get("filtre") or None))

    @app.get("/tache")
    def tache():
        return jsonify(taches.etat())

    @app.post("/ajouter")
    def ajouter():
        try:
            cid = services.ajouter_lien(g.cfg, g.depot, g.journal, request.form.get("url", ""),
                                        request.form.get("nom", ""))
        except ValueError as e:
            flash(f"Lien refusé : {e}.", "danger")
            return redirect(url_for("tableau"))
        if cid is None:
            flash("Ce concours est déjà dans la liste (doublon).", "attention")
            return redirect(url_for("tableau"))
        flash("Concours ajouté : analyse en cours.", "ok")
        lancer_tache("Analyse du concours ajouté", lambda cfg, depot, journal, log:
                     services.analyser_tout(cfg, depot, journal, log, ids=[cid]))
        return redirect(url_for("tableau", filtre="tous"))

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
        else:
            lancer_tache("Nouvelle analyse", lambda cfg, depot, journal, log:
                         services.analyser_tout(cfg, depot, journal, log, ids=[cid]))
            flash("Nouvelle analyse lancée (résultat sur le tableau de bord).", "ok")
        return redirect(url_for("concours", cid=cid))

    # ---------------------------------------------------------- participation
    def _checklist(r):
        return checklist_avant_inscription(r, g.cfg.profil, g.cfg.filtres, g.depot, date.today(),
                                           VERSION_CONSENTEMENT)

    @app.get("/concours/<int:cid>/participer")
    def participer(cid: int):
        r = concours_ou_404(cid)
        points = _checklist(r)
        return render_template(
            "participer.html", r=r, automatiques=[p for p in points if p.ok is not None],
            manuels=[(i, p) for i, p in enumerate(points) if p.ok is None],
            bloquants=[p for p in points if p.ok is False],
            prefill=r["mode_assistance"] == Mode.PREREMPLISSAGE.value, ecran=affichage_disponible())

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
        if mode == Mode.PREREMPLISSAGE and not affichage_disponible():
            flash("Pas d'écran disponible pour un navigateur visible : passage en mode manuel.", "attention")
            mode = Mode.MANUEL
        g.journal.evenement("participation_debut", concours_id=cid, domaine=domaine(url), mode=mode.value)
        checklists_validees[cid] = mode
        if mode == Mode.PREREMPLISSAGE:
            ancienne = participations.get(cid)
            if not (ancienne and ancienne.active):
                participations[cid] = Participation(cid, url, g.cfg.profil.champs_transmissibles(), g.cfg)
        return redirect(url_for("suivi", cid=cid))

    @app.get("/concours/<int:cid>/suivi")
    def suivi(cid: int):
        r = concours_ou_404(cid)
        mode = checklists_validees.get(cid)
        if mode is None:  # pas de checklist validée : on y retourne
            return redirect(url_for("participer", cid=cid))
        return render_template("suivi.html", r=r, mode=mode.value, url=r["url_participation"] or r["url"],
                               valeurs=g.cfg.profil.champs_transmissibles(), p=participations.get(cid))

    @app.get("/concours/<int:cid>/suivi/etat")
    def suivi_etat(cid: int):
        p = participations.get(cid)
        if p is None:
            return jsonify({"messages": [], "active": False, "erreur": None})
        return jsonify({"messages": p.messages[-30:], "active": p.active, "erreur": p.erreur})

    @app.post("/concours/<int:cid>/basculer-manuel")
    def basculer_manuel(cid: int):
        concours_ou_404(cid)
        if cid in checklists_validees:
            p = participations.pop(cid, None)
            if p:
                p.terminer(False)
            checklists_validees[cid] = Mode.MANUEL
        return redirect(url_for("suivi", cid=cid))

    @app.post("/concours/<int:cid>/terminer")
    def terminer(cid: int):
        r = concours_ou_404(cid)
        mode = checklists_validees.pop(cid, None)
        if mode is None:
            return redirect(url_for("participer", cid=cid))
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
            flash(f"Pré-remplissage autorisé sur {nom}. Relance l'analyse des concours concernés.", "ok")
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
        return render_template("configuration.html", cfg=g.cfg)

    @app.post("/profil")
    def configuration_enregistrer():
        f = request.form
        try:
            age = int(f.get("age") or 0)
            sections = {
                "profil": {**{c: f.get(c, "").strip() for c in
                              ("email", "prenom", "nom", "code_postal", "ville", "telephone", "date_naissance")},
                           "age": age, "pays_residence": (f.get("pays_residence") or "FR").strip().upper()},
                "filtres": {"valeur_minimale_eur": float((f.get("valeur_minimale_eur") or "100").replace(",", ".")),
                            "critere_valeur": f.get("critere_valeur", "lot_principal"),
                            "jours_restants_minimum": int(f.get("jours_restants_minimum") or 0)},
                "collecte.newsletter": {"active": f.get("newsletter_active") == "1",
                                        "utilisateur": f.get("newsletter_utilisateur", "").strip(),
                                        "dossier": f.get("newsletter_dossier", "").strip() or "Concours"},
            }
            enregistrer_config(chemin_config, sections)
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
        return render_template("donnees.html", cfg=g.cfg, actif=g.depot.consentement_actif(VERSION_CONSENTEMENT))

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
        if taches.etat()["en_cours"] or any(p.active for p in participations.values()):
            flash("Attends la fin de la tâche ou de la participation en cours.", "attention")
            return redirect(url_for("donnees"))
        g.pop("depot").fermer()
        purger_tout(g.cfg)
        checklists_validees.clear()
        participations.clear()
        flash("Toutes tes données ont été effacées (base, journal, preuves).", "ok")
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
