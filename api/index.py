"""Point d'entrée Vercel (version en ligne). Toutes les URL sont redirigées ici (voir vercel.json)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from concours_assistant.web import creer_app  # noqa: E402

app = creer_app(None)
