#!/bin/bash
# Assistant concours : double-clique sur ce fichier (Mac) pour ouvrir le site.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python n'est pas installé : installe Python 3.11+ depuis https://www.python.org/downloads/"
  read -r -p "Appuie sur Entrée pour fermer."
  exit 1
fi
if [ ! -f .venv/installe.ok ]; then
  echo "Première installation : patiente quelques minutes…"
  if python3 -m venv .venv && .venv/bin/python -m pip install -e ".[navigateur]" && .venv/bin/python -m playwright install chromium; then
    touch .venv/installe.ok
  else
    echo "L'installation a échoué. Vérifie Python 3.11+ et ta connexion Internet."
    read -r -p "Appuie sur Entrée pour fermer."
    exit 1
  fi
fi
.venv/bin/python -m concours_assistant site
