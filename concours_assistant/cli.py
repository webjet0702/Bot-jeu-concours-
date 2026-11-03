"""Interface en ligne de commande : `concours <commande>` (ou `python -m concours_assistant`)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from .analyse import AReporter, analyser, enregistrer_decision
from .config import Config, ErreurConfig, charger_config, modele_config
from .conformite import checklist_avant_inscription, evaluer
from .db import BLOCAGE_HUMAIN, Depot, maintenant
from .detection import DEFI_ACCES
from .http_poli import ClientPoli, ErreurAcces, QuotaAtteint
from .journal import Journal
from .models import Contexte, InfosExtraites, Mode, Statut
from .prefill import affichage_disponible, participation_manuelle, preremplir_et_attendre
from .sources.allowlist import lire_allowlist
from .sources.newsletter import collecter_newsletters
from .sources.rss import collecter_flux
from .urls import domaine
from .vie_privee import VERSION_CONSENTEMENT, exporter, purger_anciens, purger_tout, texte_consentement

MODELE_ALLOWLIST = """\
# Une URL de concours par ligne, format : URL | nom facultatif
# N'ajoutez que des concours que vous avez trouvés vous-même.
# https://www.exemple-marque.fr/jeu-concours-noel | Grand jeu de Noël Exemple
"""

STATUTS_ACTIFS = [Statut.PRET.value, Statut.A_VERIFIER.value, Statut.INTERVENTION_HUMAINE.value]


def _oui(question: str) -> bool:
    return input(question).strip().lower() in ("o", "oui", "y", "yes")


class Contexte_CLI:
    def __init__(self, args: argparse.Namespace):
        self.cfg: Config = charger_config(args.config)
        self.depot = Depot(self.cfg.chemin_base)
        self.journal = Journal(self.depot)
        self.aujourd_hui = date.today()

    def exiger_consentement(self) -> None:
        if not self.depot.consentement_actif(VERSION_CONSENTEMENT):
            raise ErreurConfig("consentement absent ou retiré : lancez `concours consentement`")


# ---------------------------------------------------------------- commandes


def cmd_init(args) -> int:
    chemin = Path(args.config)
    if not chemin.exists():
        chemin.write_text(modele_config(), encoding="utf-8")
        print(f"✓ {chemin} créé : renseignez au minimum `email` et `age` dans [profil].")
    cfg = charger_config(chemin)
    if not cfg.chemin_allowlist.exists():
        cfg.chemin_allowlist.write_text(MODELE_ALLOWLIST, encoding="utf-8")
        print(f"✓ {cfg.chemin_allowlist} créé.")
    depot = Depot(cfg.chemin_base)
    print(f"✓ base de données : {cfg.chemin_base}")
    if not depot.consentement_actif(VERSION_CONSENTEMENT):
        return _demander_consentement(cfg, depot)
    return 0


def _demander_consentement(cfg: Config, depot: Depot) -> int:
    print("\n" + texte_consentement(cfg))
    if _oui("J'ai lu et j'accepte ce traitement de mes données [o/N] : "):
        depot.enregistrer_consentement(VERSION_CONSENTEMENT)
        Journal(depot).evenement("consentement_accorde", version=VERSION_CONSENTEMENT)
        print("✓ consentement enregistré.")
        return 0
    print("Consentement refusé : l'outil ne traitera aucune donnée.")
    return 1


def cmd_consentement(args) -> int:
    cfg = charger_config(args.config)
    depot = Depot(cfg.chemin_base)
    if args.retirer:
        depot.retirer_consentement()
        Journal(depot).evenement("consentement_retire")
        print("✓ consentement retiré. Pour effacer vos données : `concours purger --tout`.")
        return 0
    return _demander_consentement(cfg, depot)


def cmd_collecter(args) -> int:
    c = Contexte_CLI(args)
    c.exiger_consentement()
    client = ClientPoli(c.cfg.reseau, c.depot, c.journal)
    candidats = []
    for flux in c.cfg.collecte.flux:
        try:
            trouves = collecter_flux(flux, client)
            candidats += trouves
            print(f"  flux « {flux.nom} » : {len(trouves)} entrée(s)")
        except QuotaAtteint as e:
            print(f"  ⚠ {e.message}")
            if e.global_:
                break
        except ErreurAcces as e:
            print(f"  ⚠ flux « {flux.nom} » ignoré : {e.message}")
            c.journal.evenement("flux_ignore", domaine=domaine(flux.url), raison=e.message)
    allow = lire_allowlist(c.cfg.chemin_allowlist)
    candidats += allow
    print(f"  allowlist : {len(allow)} URL(s)")
    nl = c.cfg.collecte.newsletter
    if nl.active:
        mdp = os.environ.get("CONCOURS_IMAP_MDP")
        if not mdp:
            print("  ⚠ newsletter : variable d'environnement CONCOURS_IMAP_MDP absente, ignorée")
        else:
            try:
                trouves = collecter_newsletters(nl, mdp)
                candidats += trouves
                print(f"  newsletters : {len(trouves)} lien(s)")
            except Exception as e:  # IMAP indisponible : on continue sans
                print(f"  ⚠ newsletter : {type(e).__name__}: {e}")
                c.journal.evenement("newsletter_erreur", erreur=type(e).__name__)

    nouveaux = sum(1 for cand in candidats if c.depot.ajouter_concours(cand) is not None)
    c.journal.evenement("collecte", candidats=len(candidats), nouveaux=nouveaux, doublons=len(candidats) - nouveaux)
    print(f"✓ {nouveaux} nouveau(x) concours, {len(candidats) - nouveaux} doublon(s) ignoré(s).")
    print("  Étape suivante : `concours analyser`")
    return 0


def cmd_analyser(args) -> int:
    c = Contexte_CLI(args)
    c.exiger_consentement()
    client = ClientPoli(c.cfg.reseau, c.depot, c.journal)
    if args.id:
        rows = [r for r in (c.depot.obtenir(i) for i in args.id) if r is not None]
    else:
        statuts = [Statut.NOUVEAU.value] + ([Statut.ERREUR.value] if args.reessayer else [])
        rows = c.depot.lister(statuts)[: args.limite]
    if not rows:
        print("Rien à analyser.")
        return 0
    for row in rows:
        try:
            d = analyser(row, client=client, depot=c.depot, journal=c.journal, cfg=c.cfg, aujourd_hui=c.aujourd_hui)
        except AReporter as e:
            print(f"  #{row['id']} reporté : {e}")
            if e.global_:
                break
            continue
        print(f"  #{row['id']:<4} {d.statut.value:<21} {d.mode.value:<15} {row['nom'][:50]}")
        for m in d.motifs[:3]:
            print(f"         - {m}")
    print("✓ Analyse terminée. `concours lister` pour voir les concours prêts.")
    return 0


def _fmt_eur(v) -> str:
    return f"{v:,.0f} €".replace(",", " ") if v is not None else "?"


def cmd_lister(args) -> int:
    c = Contexte_CLI(args)
    statuts = None if args.tous else (args.statut or STATUTS_ACTIFS)
    rows = c.depot.lister(statuts)
    if not rows:
        print("Aucun concours.")
        return 0
    print(f"{'ID':>4}  {'STATUT':<21} {'MODE':<15} {'GAIN':>9}  {'FIN':<10}  NOM")
    for r in rows:
        print(f"{r['id']:>4}  {r['statut']:<21} {(r['mode_assistance'] or '-'):<15} "
              f"{_fmt_eur(r['gain_principal_eur']):>9}  {(r['date_limite'] or '?'):<10}  {r['nom'][:60]}")
    return 0


def cmd_voir(args) -> int:
    c = Contexte_CLI(args)
    r = c.depot.obtenir(args.id)
    if r is None:
        print("Concours introuvable.")
        return 1
    print(f"#{r['id']} — {r['nom']}")
    for cle in ("statut", "mode_assistance", "source", "url", "url_participation", "url_reglement",
                "date_limite", "age_minimum", "date_decouverte", "date_inscription", "preuve"):
        print(f"  {cle:<18} {r[cle] if r[cle] is not None else '-'}")
    print(f"  {'gain principal':<18} {_fmt_eur(r['gain_principal_eur'])}")
    print(f"  {'dotation totale':<18} {_fmt_eur(r['dotation_totale_eur'])}")
    for titre, cle in (("Motifs", "motifs"), ("Avertissements", "avertissements")):
        elements = json.loads(r[cle] or "[]")
        if elements:
            print(f"  {titre} :")
            for e in elements:
                print(f"    - {e}")
    infos = json.loads(r["conditions"] or "{}").get("infos", {})
    if infos.get("preuve_interdiction"):
        print(f"  Clause anti-automatisation : « {infos['preuve_interdiction']} »")
    return 0


def cmd_participer(args) -> int:
    c = Contexte_CLI(args)
    c.exiger_consentement()
    r = c.depot.obtenir(args.id)
    if r is None:
        print("Concours introuvable.")
        return 1
    points = checklist_avant_inscription(r, c.cfg.profil, c.cfg.filtres, c.depot, c.aujourd_hui,
                                         VERSION_CONSENTEMENT)
    print(f"\nCHECKLIST DE CONFORMITÉ — #{r['id']} {r['nom']}")
    for p in points:
        icone = {True: "✓", False: "✗", None: "?"}[p.ok]
        print(f"  {icone} {p.libelle}" + (f"  [{p.detail}]" if p.detail else ""))
    bloquants = [p for p in points if p.ok is False]
    if bloquants:
        c.journal.evenement("checklist_echec", concours_id=r["id"], points=[p.libelle for p in bloquants])
        print("\n⛔ Inscription impossible tant que les points ✗ ne sont pas résolus.")
        return 1
    print("\nConfirmez chaque point (o = oui) :")
    for p in points:
        if p.ok is None and not _oui(f"  ? {p.libelle} [o/N] "):
            c.journal.evenement("checklist_refusee", concours_id=r["id"], point=p.libelle)
            print("Abandon : rien n'a été envoyé.")
            return 1
    c.journal.evenement("checklist_validee", concours_id=r["id"])

    url = r["url_participation"] or r["url"]
    valeurs = c.cfg.profil.champs_transmissibles()
    mode = Mode.MANUEL if args.manuel else Mode(r["mode_assistance"])
    if mode == Mode.PREREMPLISSAGE and not affichage_disponible():
        print("Aucun écran disponible : le navigateur doit être VISIBLE, passage en mode manuel.")
        mode = Mode.MANUEL
    c.journal.evenement("participation_debut", concours_id=r["id"], domaine=domaine(url), mode=mode.value)

    if mode == Mode.PREREMPLISSAGE:
        try:
            res = preremplir_et_attendre(url, valeurs, concours_id=r["id"], journal=c.journal,
                                         dossier_preuves=c.cfg.dossier_preuves,
                                         domaine_autorise=c.depot.domaine_autorise)
        except Exception as e:  # navigateur absent, page injoignable...
            c.journal.evenement("preremplissage_erreur", concours_id=r["id"], erreur=type(e).__name__)
            print(f"⚠ Pré-remplissage impossible ({type(e).__name__}: {e}).\n"
                  "  Installez le navigateur (`playwright install chromium`) ou relancez avec --manuel.")
            return 1
        if res.arret and res.arret.type == DEFI_ACCES:
            c.depot.bloquer_domaine(domaine(url), BLOCAGE_HUMAIN, f"défi anti-robot ({res.arret.fournisseur})")
    else:
        res = participation_manuelle(url, valeurs)

    if res.envoye:
        c.depot.mettre_a_jour(r["id"], statut=Statut.INSCRIT.value, date_inscription=maintenant(), preuve=res.preuve)
        c.journal.evenement("inscription_confirmee", concours_id=r["id"], domaine=domaine(url), mode=mode.value,
                            preuve=res.preuve)
        print(f"✓ Inscription enregistrée (preuve : {res.preuve}).")
    else:
        c.journal.evenement("inscription_non_envoyee", concours_id=r["id"])
        if _oui("Abandonner définitivement ce concours ? [o/N] "):
            c.depot.mettre_a_jour(r["id"], statut=Statut.ABANDONNE.value)
        print("Rien n'a été enregistré comme envoyé.")
    return 0


def cmd_corriger(args) -> int:
    c = Contexte_CLI(args)
    c.exiger_consentement()
    r = c.depot.obtenir(args.id)
    if r is None:
        print("Concours introuvable.")
        return 1
    cond = json.loads(r["conditions"] or "{}")
    infos = InfosExtraites.depuis_dict(cond.get("infos", {}))
    ctx = Contexte.depuis_dict(cond.get("contexte", {}))
    modifs = {}
    if args.valeur is not None:
        infos.gain_principal_eur = modifs["gain_principal_eur"] = args.valeur
    if args.dotation is not None:
        infos.dotation_totale_eur = modifs["dotation_totale_eur"] = args.dotation
        infos.dotation_estimee = False
    if args.date_limite:
        infos.date_limite = date.fromisoformat(args.date_limite)
        modifs["date_limite"] = args.date_limite
    if args.age_min is not None:
        infos.age_minimum = modifs["age_minimum"] = args.age_min or None
    if args.reglement:
        infos.url_reglement = modifs["url_reglement"] = args.reglement
        ctx.reglement_trouve = ctx.reglement_lu = True
    if not modifs:
        print("Aucune correction fournie.")
        return 1
    url = r["url_participation"] or r["url"]
    ctx.domaine_automatisation_autorisee = c.depot.domaine_autorise(domaine(url))
    d = evaluer(infos, ctx, c.cfg.profil, c.cfg.filtres, c.aujourd_hui)
    enregistrer_decision(c.depot, r["id"], infos, ctx, d, url)
    c.journal.evenement("correction_manuelle", concours_id=r["id"], champs=modifs, statut=d.statut.value)
    print(f"✓ #{r['id']} → {d.statut.value} ({d.mode.value})")
    for m in d.motifs:
        print(f"   - {m}")
    return 0


def cmd_statut(args) -> int:
    c = Contexte_CLI(args)
    r = c.depot.obtenir(args.id)
    if r is None:
        print("Concours introuvable.")
        return 1
    if r["statut"] == Statut.INSCRIT.value:
        print("Déjà inscrit : statut non modifiable.")
        return 1
    champs = {"statut": args.nouveau}
    if args.nouveau == Statut.PRET.value:
        if r["statut"] == Statut.EXCLU.value:
            print("⚠ Ce concours avait été EXCLU par une règle (voir `concours voir`). "
                  "Les contrôles durs (gain, date, HTTPS, blocage) restent appliqués à l'inscription.")
        if r["mode_assistance"] in (None, Mode.ABSTENTION.value):
            champs["mode_assistance"] = Mode.MANUEL.value
    c.depot.mettre_a_jour(r["id"], **champs)
    c.journal.evenement("statut_force", concours_id=r["id"], ancien=r["statut"], nouveau=args.nouveau,
                        motif=args.motif)
    print(f"✓ #{r['id']} : {r['statut']} → {args.nouveau}")
    return 0


def cmd_domaine(args) -> int:
    c = Contexte_CLI(args)
    if args.action == "lister":
        for d in c.depot.lister_domaines():
            etat = "AUTORISÉ" if d["automatisation_autorisee"] else "manuel"
            bloc = f" | BLOQUÉ : {d['raison_blocage']}" if c.depot.domaine_bloque(d["domaine"]) else ""
            print(f"  {d['domaine']:<35} {etat:<9}{bloc}  {d['justification'] or ''}")
        return 0
    if not args.domaine:
        print("Précisez le domaine.")
        return 1
    nom = domaine(args.domaine if "://" in args.domaine else f"https://{args.domaine}")
    if args.action == "autoriser":
        if not args.justification or len(args.justification.strip()) < 15:
            print("Justification obligatoire (≥ 15 caractères) : citez l'article des CGU/du règlement "
                  "qui autorise les outils d'aide à la saisie.")
            return 1
        print("Rappel : n'autorisez un domaine que si ses CGU/règlements AUTORISENT l'assistance automatisée.")
        c.depot.autoriser_domaine(nom, args.justification.strip())
        c.journal.evenement("domaine_autorise", domaine=nom, justification=args.justification.strip())
    elif args.action == "retirer":
        c.depot.revoquer_domaine(nom)
        c.journal.evenement("domaine_revoque", domaine=nom)
    elif args.action == "debloquer":
        c.depot.debloquer_domaine(nom)
        c.journal.evenement("domaine_debloque", domaine=nom, par="humain")
        print("Pensez à relancer `concours analyser --id <ID>` pour les concours concernés.")
    print(f"✓ {nom} : {args.action}")
    return 0


def cmd_journal(args) -> int:
    c = Contexte_CLI(args)
    for e in reversed(c.depot.lire_journal(args.n)):
        cible = f" #{e['concours_id']}" if e["concours_id"] else ""
        dom = f" [{e['domaine']}]" if e["domaine"] else ""
        print(f"{e['horodatage']}  {e['type']:<28}{cible}{dom}  {e['details']}")
    return 0


def cmd_exporter(args) -> int:
    c = Contexte_CLI(args)
    chemin = exporter(c.depot, c.cfg, Path(args.fichier))
    c.journal.evenement("export_donnees", fichier=str(chemin))
    print(f"✓ données exportées dans {chemin}")
    return 0


def cmd_purger(args) -> int:
    cfg = charger_config(args.config)
    if args.tout:
        if not _oui(f"Effacer DÉFINITIVEMENT {cfg.dossier_donnees} (base, journal, preuves) ? [o/N] "):
            return 1
        purger_tout(cfg)
        print(f"✓ données effacées. Supprimez aussi {args.config} et {cfg.chemin_allowlist} si vous le souhaitez.")
        return 0
    depot = Depot(cfg.chemin_base)
    compte = purger_anciens(depot, cfg)
    Journal(depot).evenement("purge", **compte)
    print(f"✓ purge (> {cfg.vie_privee.retention_jours} jours) : {compte}")
    return 0


# ------------------------------------------------------------------ parseur


def construire_parseur() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="concours", description="Assistant jeux-concours conforme (validation humaine).")
    p.add_argument("--config", default="config.toml", help="chemin du fichier de configuration")
    p.add_argument("-v", "--verbeux", action="store_true")
    sp = p.add_subparsers(dest="commande", required=True)

    sp.add_parser("init", help="créer la configuration, la base et recueillir le consentement").set_defaults(f=cmd_init)
    s = sp.add_parser("consentement", help="donner ou retirer son consentement")
    s.add_argument("--retirer", action="store_true")
    s.set_defaults(f=cmd_consentement)
    sp.add_parser("collecter", help="RSS + newsletters + allowlist").set_defaults(f=cmd_collecter)
    s = sp.add_parser("analyser", help="extraire gains/règles, filtrer, vérifier la conformité")
    s.add_argument("--limite", type=int, default=20)
    s.add_argument("--id", type=int, nargs="*")
    s.add_argument("--reessayer", action="store_true", help="réanalyser aussi les concours en erreur")
    s.set_defaults(f=cmd_analyser)
    s = sp.add_parser("lister", help="concours prêts / à vérifier")
    s.add_argument("--statut", nargs="*", choices=[x.value for x in Statut])
    s.add_argument("--tous", action="store_true")
    s.set_defaults(f=cmd_lister)
    s = sp.add_parser("voir", help="détails d'un concours")
    s.add_argument("id", type=int)
    s.set_defaults(f=cmd_voir)
    s = sp.add_parser("participer", help="checklist, pré-remplissage, validation humaine")
    s.add_argument("id", type=int)
    s.add_argument("--manuel", action="store_true", help="ne rien automatiser : ouvrir le lien seulement")
    s.set_defaults(f=cmd_participer)
    s = sp.add_parser("corriger", help="corriger une valeur extraite puis réévaluer")
    s.add_argument("id", type=int)
    s.add_argument("--valeur", type=float, help="valeur du lot principal (€)")
    s.add_argument("--dotation", type=float, help="dotation totale (€)")
    s.add_argument("--date-limite", help="AAAA-MM-JJ")
    s.add_argument("--age-min", type=int, help="âge minimum (0 = aucun)")
    s.add_argument("--reglement", help="URL du règlement lu par vous")
    s.set_defaults(f=cmd_corriger)
    s = sp.add_parser("statut", help="changer le statut après vérification humaine")
    s.add_argument("id", type=int)
    s.add_argument("nouveau", choices=[Statut.PRET.value, Statut.A_VERIFIER.value, Statut.ABANDONNE.value,
                                       Statut.NOUVEAU.value])
    s.add_argument("--motif", required=True, help="ce que vous avez vérifié")
    s.set_defaults(f=cmd_statut)
    s = sp.add_parser("domaine", help="autorisations d'assistance et blocages par domaine")
    s.add_argument("action", choices=["autoriser", "retirer", "debloquer", "lister"])
    s.add_argument("domaine", nargs="?")
    s.add_argument("--justification")
    s.set_defaults(f=cmd_domaine)
    s = sp.add_parser("journal", help="journal d'audit")
    s.add_argument("-n", type=int, default=40)
    s.set_defaults(f=cmd_journal)
    s = sp.add_parser("exporter", help="export JSON de toutes vos données (droit d'accès)")
    s.add_argument("fichier")
    s.set_defaults(f=cmd_exporter)
    s = sp.add_parser("purger", help="supprimer les données anciennes (ou tout avec --tout)")
    s.add_argument("--tout", action="store_true")
    s.set_defaults(f=cmd_purger)
    return p


def main(argv: list[str] | None = None) -> int:
    args = construire_parseur().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbeux else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        return args.f(args)
    except ErreurConfig as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrompu : rien n'a été envoyé.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
