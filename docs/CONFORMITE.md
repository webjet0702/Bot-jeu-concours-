# Conformité

> Ce document n'est pas un avis juridique. Il décrit comment l'outil applique les règles
> fixées au départ du projet.

## 1. Règles absolues et où elles sont appliquées

| Règle | Mécanisme | Fichier |
|---|---|---|
| Ne jamais contourner/tester la détection de robot | Aucun code de contournement. `detection.py` ne sert qu'à **s'arrêter**. | `detection.py` |
| Ne jamais résoudre un CAPTCHA | Contrôle avant chaque action ; si CAPTCHA → arrêt, la main passe à l'humain. | `prefill.py` |
| Pas de navigateur furtif, faux fingerprint, proxies | `chromium.launch(headless=False)` sans autre option ; User-Agent HTTP honnête, imposé par la validation de la config. | `prefill.py`, `config.py` |
| Respect de robots.txt | Vérifié pour chaque URL et chaque redirection ; en cas de doute, tout est interdit. | `http_poli.py` |
| Respect des limites de débit | Délai par domaine, Crawl-delay, quotas, 429 = pause, aucun parallélisme. | `http_poli.py` |
| Respect des CGU / règlements | Clause anti-automatisation détectée → mode manuel ; pré-remplissage seulement sur domaine autorisé **par vous** avec justification. | `extraction.py`, `conformite.py`, `cli.py` |
| Arrêt sur challenge + journal + humain | `DefiAntiBot` → domaine bloqué jusqu'à déblocage manuel, événements `defi_anti_bot` / `intervention_humaine_requise`. | `http_poli.py`, `analyse.py` |
| Inscription finale confirmée par vous | L'outil ne clique jamais « Valider » ni ne coche de case ; vous confirmez l'envoi dans le terminal. | `prefill.py` |
| Consentement, minimisation, suppression, journalisation | Voir § 4. | `vie_privee.py`, `journal.py` |

## 2. Checklist avant chaque inscription

Exécutée par `concours participer ID`. Un seul ✗ bloque l'inscription.

**Contrôles automatiques (bloquants)**
- [ ] Consentement enregistré (version courante)
- [ ] Statut « prêt »
- [ ] Pas déjà inscrit
- [ ] Gain ≥ 100 € (recontrôlé même si le statut a été forcé)
- [ ] Date limite non dépassée
- [ ] Domaine sans blocage ni défi anti-robot en cours
- [ ] Connexion HTTPS
- [ ] Mode d'assistance défini ; si pré-remplissage : domaine autorisé
- [ ] Données minimales : liste des seuls champs qui seront proposés

**Confirmations humaines (o/N)**
- [ ] J'ai lu le règlement
- [ ] L'organisateur est légitime (site/compte officiel) et aucun paiement n'est demandé
- [ ] Je remplis les conditions (âge, pays de résidence, une participation par personne/foyer)
- [ ] J'accepte que ces champs soient transmis à l'organisateur pour ce concours
- [ ] (si non trouvée) La date limite n'est pas dépassée

## 3. Cas où l'outil s'abstient

**Exclusion (aucune action, statut `exclu`)**
1. Gain < 100 € (ou < seuil configuré, qui ne peut pas être inférieur à 100 €).
2. Date limite dépassée ou trop proche (`jours_restants_minimum`).
3. Âge insuffisant sans possibilité d'autorisation parentale.
4. Demande de paiement, de carte bancaire, d'IBAN, de « frais de port/dossier », numéro ou SMS surtaxé, Western Union, PaySafeCard, crypto…
5. Participation conditionnée à un achat.
6. Page en HTTP (non chiffrée).
7. Page introuvable (404/410).
8. Doublon d'un concours déjà connu.

**Pas d'automatisation, vérification humaine (statut `a_verifier` ou `intervention_humaine`)**

9. Défi anti-robot, CAPTCHA d'accès, page « vérifiez que vous êtes humain ».
10. HTTP 401/403, ou domaine bloqué après un défi précédent.
11. HTTP 429 : pause, réessai plus tard.
12. robots.txt interdit l'URL, ou robots.txt injoignable.
13. Concours sur réseau social (Instagram, Facebook, TikTok, X, YouTube, etc.).
14. Valeur du gain introuvable.
15. Règlement introuvable.
16. Âge inconnu alors qu'un âge minimum est exigé, ou autorisation parentale nécessaire.

**Mode manuel imposé (l'outil ouvre le lien, vous faites tout)**

17. Le règlement interdit les moyens automatisés (cas le plus fréquent).
18. Un CAPTCHA est présent dans le formulaire.
19. Le domaine n'a pas été autorisé par vous après lecture de ses CGU.
20. Aucun écran disponible (le navigateur doit être visible).

## 4. RGPD

- **Cadre** : l'outil est un usage personnel, les données restent sur votre ordinateur
  (exemption « domestique », art. 2.2.c du RGPD). Il applique malgré tout les bonnes
  pratiques ci-dessous. Dès que vous cliquez « Valider », c'est **l'organisateur** qui
  devient responsable du traitement de vos données.
- **Consentement** : demandé à `concours init`, versionné, retirable
  (`concours consentement --retirer`). Sans consentement, aucune commande de traitement
  ne s'exécute.
- **Minimisation** : seuls les champs renseignés dans `[profil]` sont proposés (par
  défaut : l'email). L'âge sert uniquement au filtrage et n'est jamais envoyé. Les
  champs ambigus ne sont pas remplis.
- **Journal** : emails et téléphones masqués automatiquement.
- **Conservation** : 180 jours par défaut, puis `concours purger`. Les preuves orphelines
  sont supprimées.
- **Droits** : accès/portabilité (`concours exporter fichier.json`), effacement
  (`concours purger --tout`).
- **Mineurs** : en France, l'accord d'un parent est nécessaire avant 15 ans pour
  consentir au traitement de ses données par un service en ligne. Beaucoup de
  concours sont réservés aux majeurs : renseignez votre âge réel.
- **Hygiène** : `config.toml`, `allowlist.txt` et `donnees/` sont exclus de git.
  Le mot de passe IMAP n'est lu que depuis une variable d'environnement.

## 5. Interdictions explicites (et définitives)

Ce dépôt ne contient et n'acceptera aucun code pour :
- contourner, résoudre ou faire résoudre un CAPTCHA (reCAPTCHA, hCaptcha, Turnstile, Friendly Captcha, Arkose…) ;
- passer Cloudflare, DataDome, Akamai, PerimeterX/HUMAN, Imperva ou tout autre anti-bot ;
- masquer l'automatisation (stealth, faux User-Agent, empreinte modifiée, `navigator.webdriver`, proxies tournants, mouvements « humains ») ;
- extraire massivement un site dont les conditions l'interdisent.
