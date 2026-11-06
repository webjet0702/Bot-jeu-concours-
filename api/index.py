"""Point d'entrée Vercel (version en ligne). Toutes les URL sont redirigées ici (voir vercel.json)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if (os.environ.get("CONCOURS_DATABASE_URL") or "").strip():
    from concours_assistant.web import creer_app  # noqa: E402

    app = creer_app(None)
else:  # pas encore configuré : on explique quoi faire au lieu de planter
    from flask import Flask  # noqa: E402

    app = Flask(__name__)

    @app.route("/", defaults={"chemin": ""})
    @app.route("/<path:chemin>")
    def installation_incomplete(chemin):
        return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<title>Installation à terminer</title><body style='font-family:system-ui;max-width:560px;"
                "margin:40px auto;padding:0 16px;line-height:1.5'><h1>Presque prêt</h1><p>Il manque la variable "
                "<code>CONCOURS_DATABASE_URL</code> dans les réglages Vercel du projet (Settings → Environment "
                "Variables), puis un redéploiement. Suis la section « Mettre le site en ligne » du README.</p>"
                ), 503
