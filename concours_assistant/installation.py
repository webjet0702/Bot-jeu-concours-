"""Installation de la version en ligne : SQL de création du schéma et du rôle applicatif.

Le mot de passe du rôle n'est jamais envoyé en clair à la base : on transmet seulement son
empreinte SCRAM-SHA-256 (format accepté directement par PostgreSQL).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string

from .db import Depot

ROLE_APPLICATION = "concours_app"
ALPHABET = string.ascii_letters + string.digits


def mot_de_passe_aleatoire(longueur: int = 40) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(longueur))


def verificateur_scram(mot_de_passe: str, iterations: int = 4096, sel: bytes | None = None) -> str:
    sel = sel or secrets.token_bytes(16)
    sale = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode("utf-8"), sel, iterations)
    cle_client = hmac.new(sale, b"Client Key", hashlib.sha256).digest()
    cle_stockee = hashlib.sha256(cle_client).digest()
    cle_serveur = hmac.new(sale, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(sel)}${b64(cle_stockee)}:{b64(cle_serveur)}"


def sql_installation(verificateur: str, role: str = ROLE_APPLICATION) -> str:
    if not verificateur.startswith("SCRAM-SHA-256$") or "'" in verificateur:
        raise ValueError("vérificateur SCRAM invalide")
    return Depot.schema(pg=True) + f"""
REVOKE ALL ON SCHEMA concours FROM PUBLIC;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} LOGIN PASSWORD '{verificateur}';
  ELSE
    ALTER ROLE {role} LOGIN PASSWORD '{verificateur}';
  END IF;
END $$;
ALTER ROLE {role} SET search_path = concours;
GRANT USAGE ON SCHEMA concours TO {role};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA concours TO {role};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA concours TO {role};
ALTER DEFAULT PRIVILEGES IN SCHEMA concours GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role};
ALTER DEFAULT PRIVILEGES IN SCHEMA concours GRANT USAGE, SELECT ON SEQUENCES TO {role};
"""
