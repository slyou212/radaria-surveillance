@echo off
REM =====================================================
REM  RadarIA — Demarrage automatique au boot Windows
REM  Installe automatiquement par SETUP_RADARIA.bat dans :
REM  %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
REM =====================================================
title RadarIA — Auto-demarrage

REM Attendre que le reseau soit disponible (45s)
echo [RadarIA] Attente connexion reseau (45s)...
timeout /t 45 /nobreak >nul

REM Calculer le dossier racine RadarIA (parent de ce BAT ou C:\radaria-client)
set ROOT_DIR=%~dp0

REM Lancer la surveillance en arriere-plan via PowerShell (compatible session non-interactive)
echo [RadarIA] Lancement surveillance...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'python' -ArgumentList '%ROOT_DIR%RadarIA_PC\surveillance.py' -WorkingDirectory '%ROOT_DIR%RadarIA_PC' -WindowStyle Minimized"

REM Attendre 5s puis lancer le gardien en daemon
timeout /t 5 /nobreak >nul
echo [RadarIA] Lancement gardien...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'python' -ArgumentList '%ROOT_DIR%gardien_radaria.py --daemon' -WorkingDirectory '%ROOT_DIR%' -WindowStyle Minimized"

echo [RadarIA] Systeme lance avec succes.
