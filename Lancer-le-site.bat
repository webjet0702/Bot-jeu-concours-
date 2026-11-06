@echo off
rem Assistant concours : double-clique sur ce fichier pour ouvrir le site.
cd /d "%~dp0"
title Assistant concours
where python >nul 2>nul
if errorlevel 1 (
  echo Python n'est pas installe.
  echo Installe Python 3.11 ou plus recent depuis https://www.python.org/downloads/
  echo en cochant "Add python.exe to PATH", puis relance ce fichier.
  pause
  exit /b 1
)
if not exist ".venv\installe.ok" (
  echo Premiere installation : patiente quelques minutes...
  python -m venv .venv || goto erreur
  ".venv\Scripts\python.exe" -m pip install -e . || goto erreur
  ".venv\Scripts\python.exe" -m playwright install chromium || goto erreur
  echo ok> ".venv\installe.ok"
)
".venv\Scripts\python.exe" -m concours_assistant site
pause
exit /b 0
:erreur
echo.
echo L'installation a echoue. Verifie que Python 3.11 ou plus recent est installe
echo et que tu es connecte a Internet, puis relance ce fichier.
pause
exit /b 1
