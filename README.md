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

## Installation

```bash
python -m venv .venv && source .venv/bin/activate     # Windows : .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium                           # pour le pré-remplissage
```

## Démarrage rapide

```bash
concours init                 # crée config.toml + allowlist.txt + base, demande le consentement
# éditez config.toml : email (dédié) et age, puis ajoutez des flux RSS ou des URLs dans allowlist.txt

concours collecter            # récupère les concours (déduplication automatique)
concours analyser             # extrait gains/règles, filtre >= 100 €, contrôle la conformité
concours lister               # concours prêts / à vérifier / intervention humaine
concours voir 12              # détail, motifs, clause anti-automatisation éventuelle
concours participer 12        # checklist -> pré-remplissage ou mode manuel -> VOUS validez
```

## Toutes les commandes

| Commande | Rôle |
|---|---|
| `init` | Configuration, base, consentement |
| `consentement [--retirer]` | Donner / retirer son consentement |
| `collecter` | RSS + newsletters (IMAP lecture seule) + allowlist |
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
limitation de débit, l'extraction, la conformité, la base et le pré-remplissage sont simulés.
