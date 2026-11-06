# Assistant jeux-concours (conforme)

Un assistant **semi-automatique** pour participer à des jeux-concours en ligne :

- il **trouve** des concours (flux RSS, newsletters, liste manuelle) ;
- il **garde uniquement ceux dont le gain est ≥ 100 €** ;
- il **écarte les arnaques** (paiement demandé, numéro surtaxé, achat obligatoire, HTTP…) ;
- il **vérifie la conformité** (robots.txt, règlement, âge, date limite) ;
- il **pré-remplit** le formulaire quand c'est autorisé, dans un navigateur visible ;
- **vous** cochez les cases, faites le CAPTCHA s'il y en a un et cliquez « Valider ».

Il **ne contourne jamais** une protection anti-robot ni un CAPTCHA : dès qu'il en voit
un, il s'arrête, le note dans le journal et vous rend la main.

📐 [Architecture et workflow](docs/ARCHITECTURE.md) · ✅ [Conformité, checklist, cas d'abstention, RGPD](docs/CONFORMITE.md)

## À savoir avant de commencer

- **La plupart des règlements interdisent les « moyens automatisés »**, et beaucoup de
  formulaires ont un CAPTCHA. Dans ces cas, l'outil passe en **mode manuel** : il ouvre le
  lien et vous remplissez vous-même. Son vrai gain de temps, c'est la veille, le tri
  anti-arnaque, le filtre ≥ 100 € et le suivi.
- **Utilisez une adresse email dédiée** : beaucoup d'organisateurs partagent les adresses
  avec des « partenaires ».
- **Âge** : de nombreux concours sont réservés aux majeurs. Le gagnant doit souvent prouver
  son identité. Renseignez votre âge réel pour ne pas gagner un lot qu'on vous refusera.
- **Gagner reste rare.** C'est un loisir, pas une source de revenus.

## Le plus simple : le site (double-clic)

1. Installe **Python 3.11 ou plus récent** depuis <https://www.python.org/downloads/>
   (Windows : coche **« Add python.exe to PATH »** pendant l'installation).
2. Sur GitHub : bouton vert **Code → Download ZIP**, puis décompresse le dossier.
3. Double-clique sur :
   - **Windows** : `Lancer-le-site.bat`
   - **Mac** : `Lancer-le-site.command` (la première fois : clic droit → Ouvrir)

La première fois, l'installation prend quelques minutes. Ensuite le site s'ouvre tout
seul dans ton navigateur à l'adresse <http://127.0.0.1:8765/>. **Garde la fenêtre noire
ouverte** tant que tu utilises le site (la fermer arrête le site).

Le site fonctionne **uniquement sur ton ordinateur** (adresse 127.0.0.1) : tes données ne
partent nulle part tant que tu ne cliques pas toi-même « Valider » sur un concours.

| Page | Ce que tu y fais |
|---|---|
| **Concours** | Bouton « Chercher de nouveaux concours », ajout d'un lien, liste triée (prêts, à vérifier, inscrits…), bouton « Participer ». |
| **Participer** | Contrôles automatiques + cases à cocher, puis : soit une fenêtre Chromium pré-remplit (sites autorisés), soit tu ouvres la page et copies ton email. Tu valides toi-même, puis « J'ai validé mon inscription ». |
| **Mon profil** | Email dédié, âge, pays, seuil de gain, Google Alertes. |
| **Sites** | Sites autorisés pour le pré-remplissage, sites bloqués après un défi anti-robot. |
| **Journal** | Tout ce que l'outil a fait (y compris chaque arrêt sur CAPTCHA). |
| **Mes données** | Télécharger, purger, retirer le consentement, tout effacer. |

## Installation (ligne de commande)

```bash
python -m venv .venv && source .venv/bin/activate     # Windows : .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium                           # pour le pré-remplissage
concours site                                         # ouvre le site local
```

## Démarrage rapide (ligne de commande)

```bash
concours init                 # crée config.toml + allowlist.txt + base, demande le consentement
# éditez config.toml : email (dédié) et age, puis ajoutez des flux RSS ou des URLs dans allowlist.txt

concours collecter            # récupère les concours (déduplication automatique)
concours analyser             # extrait gains/règles, filtre >= 100 €, contrôle la conformité
concours lister               # concours prêts / à vérifier / intervention humaine
concours voir 12              # détail, motifs, clause anti-automatisation éventuelle
concours participer 12        # checklist -> pré-remplissage ou mode manuel -> VOUS validez
```

## Surveiller tout le web avec Google Alertes

L'outil n'explore **pas** le web lui-même : à cette échelle c'est impossible (des
milliards de pages), les sites le bloqueraient, et ce serait du scraping massif. Google
a déjà tout exploré : on lui demande de **nous envoyer par email** les nouvelles pages de
concours, et l'outil lit ces emails (IMAP, lecture seule).

1. Connecté avec ton adresse Gmail **dédiée**, va sur <https://www.google.com/alerts>.
2. Crée une alerte par requête, par exemple :
   - `"jeu concours" "à gagner" "d'une valeur de"`
   - `"règlement du jeu" "commissaire de justice"`
   - `"jeu gratuit sans obligation d'achat"`
   - `"tirage au sort" "gagnez" "valeur commerciale"`

   Options : *Fréquence* « une fois par jour au maximum », *Langue* Français,
   *Région* France, *Nombre* « tous les résultats », *Envoyer à* ton adresse dédiée.
3. Dans Gmail, crée un filtre : *De* `googlealerts-noreply@google.com` → *Appliquer le libellé* `Concours`.
4. Active la validation en deux étapes du compte, puis crée un **mot de passe
   d'application** (Compte Google → Sécurité). Vérifie que l'IMAP est activé
   (Gmail → Paramètres → Transfert et POP/IMAP).
5. Dans `config.toml` :
   ```toml
   [collecte.newsletter]
   active = true
   utilisateur = "ton.adresse.concours@gmail.com"
   dossier = "Concours"
   ```
6. Avant `concours collecter`, donne le mot de passe d'application via une variable
   d'environnement (jamais dans un fichier) :
   - Windows (PowerShell) : `$env:CONCOURS_IMAP_MDP = "xxxx xxxx xxxx xxxx"`
   - Mac / Linux : `export CONCOURS_IMAP_MDP="xxxx xxxx xxxx xxxx"`

Les liens des alertes sont « déballés » (on garde la vraie page, pas la redirection
Google) puis analysés comme les autres. Tu peux aussi mettre dans le libellé `Concours`
les newsletters de marques et de sites de concours auxquelles tu es abonné.

**Concours d'autres pays** : grâce à `pays_residence` (FR par défaut), un concours dont le
règlement est réservé aux résidents d'autres pays (« US residents only », « résidents
belges »…) est écarté automatiquement. Les gains en $ ou £ ne sont pas convertis : ces
concours passent « à vérifier ».

## Toutes les commandes

| Commande | Rôle |
|---|---|
| `site [--port 8765] [--sans-navigateur]` | Ouvre le site local (même logique, à la souris) |
| `init` | Configuration, base, consentement |
| `consentement [--retirer]` | Donner / retirer son consentement |
| `collecter` | RSS + Google Alertes et newsletters (IMAP lecture seule) + allowlist |
| `analyser [--limite N] [--id ID…] [--reessayer]` | Analyse polie des nouveaux concours |
| `lister [--statut …] [--tous]` | Tableau des concours |
| `voir ID` | Détails |
| `participer ID [--manuel]` | Checklist + participation assistée |
| `corriger ID --valeur 150 --date-limite 2026-12-31 --age-min 18 --reglement URL` | Corriger une extraction puis réévaluer |
| `statut ID pret --motif "vérifié le règlement"` | Valider à la main un concours « à vérifier » |
| `domaine autoriser DOMAINE --justification "CGU art. X…"` | Autoriser le pré-remplissage sur un domaine **dont les CGU le permettent** |
| `domaine debloquer DOMAINE` | Lever un blocage après vérification humaine |
| `domaine lister` / `domaine retirer DOMAINE` | Gestion des domaines |
| `journal [-n 40]` | Journal d'audit |
| `exporter fichier.json` | Export de toutes vos données |
| `purger [--tout]` | Purge (durée de conservation) ou effacement total |

## Tests

```bash
pytest
```

Les tests n'utilisent pas le réseau : le client HTTP, robots.txt, les défis anti-robot, la
limitation de débit, l'extraction, la conformité, la base, le pré-remplissage et le site
(sécurité, consentement, parcours d'inscription) sont simulés.
