-- Schéma SQLite de l'assistant jeux-concours.
-- Toutes les dates/horodatages sont en ISO 8601 (UTC pour les horodatages).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS concours (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    url                         TEXT NOT NULL,              -- URL d'origine (flux RSS, newsletter, allowlist)
    url_canonique               TEXT NOT NULL UNIQUE,       -- clé de déduplication n°1
    url_participation           TEXT,                       -- page réelle du formulaire (après résolution)
    url_participation_canonique TEXT,                       -- clé de déduplication n°2 (inter-sources)
    empreinte                   TEXT NOT NULL,              -- domaine + titre normalisé (clé n°3)
    nom                         TEXT NOT NULL,
    organisateur                TEXT,
    source                      TEXT NOT NULL,              -- ex. "rss:Mon agrégateur", "allowlist", "newsletter"
    texte_source                TEXT,                       -- résumé fourni par la source (aucune donnée perso)
    gain_principal_eur          REAL,                       -- valeur du meilleur lot
    dotation_totale_eur         REAL,                       -- valeur totale des lots (explicite ou estimée)
    date_limite                 TEXT,                       -- AAAA-MM-JJ
    age_minimum                 INTEGER,
    url_reglement               TEXT,
    conditions                  TEXT NOT NULL DEFAULT '{}', -- JSON : infos extraites + contexte de conformité
    mode_assistance             TEXT CHECK (mode_assistance IN ('preremplissage', 'manuel', 'abstention')),
    motifs                      TEXT NOT NULL DEFAULT '[]', -- JSON : raisons d'exclusion / de vérification
    avertissements              TEXT NOT NULL DEFAULT '[]', -- JSON : points à vérifier par l'humain
    statut                      TEXT NOT NULL DEFAULT 'nouveau' CHECK (statut IN (
                                    'nouveau', 'a_verifier', 'exclu', 'pret',
                                    'intervention_humaine', 'inscrit', 'abandonne', 'erreur')),
    date_decouverte             TEXT NOT NULL,
    date_analyse                TEXT,
    date_inscription            TEXT,
    preuve                      TEXT,                       -- chemin de la capture d'écran ou note manuelle
    maj_le                      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_concours_statut     ON concours(statut);
CREATE INDEX IF NOT EXISTS idx_concours_empreinte  ON concours(empreinte);
CREATE INDEX IF NOT EXISTS idx_concours_url_part   ON concours(url_participation_canonique);

-- Domaines : autorisation d'assistance automatisée (vérifiée par l'humain) et blocages.
CREATE TABLE IF NOT EXISTS domaines (
    domaine                  TEXT PRIMARY KEY,
    automatisation_autorisee INTEGER NOT NULL DEFAULT 0,
    justification            TEXT,                -- ex. "CGU art. 7 : outils d'aide autorisés (vérifié le ...)"
    verifie_le               TEXT,
    bloque_jusqu_au          TEXT,                -- '9999-12-31...' = jusqu'à intervention humaine
    raison_blocage           TEXT,
    dernier_acces            REAL                 -- horodatage Unix : délai entre deux requêtes au même site
);

CREATE TABLE IF NOT EXISTS consentements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version    TEXT NOT NULL,
    accorde_le TEXT NOT NULL,
    retire_le  TEXT
);

-- Journal d'audit : aucune donnée personnelle (emails/téléphones masqués).
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    horodatage  TEXT NOT NULL,
    type        TEXT NOT NULL,
    concours_id INTEGER REFERENCES concours(id) ON DELETE SET NULL,
    domaine     TEXT,
    details     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_journal_horodatage ON journal(horodatage);

-- Paramètres : configuration (version en ligne), mot de passe (haché), verrou anti force brute.
CREATE TABLE IF NOT EXISTS parametres (
    cle    TEXT PRIMARY KEY,
    valeur TEXT NOT NULL,
    maj_le TEXT NOT NULL
);

-- robots.txt mis en cache 24 h (moins de requêtes vers les sites).
CREATE TABLE IF NOT EXISTS robots_cache (
    origine     TEXT PRIMARY KEY,
    statut      INTEGER NOT NULL,
    contenu     TEXT,
    recupere_le TEXT NOT NULL
);
