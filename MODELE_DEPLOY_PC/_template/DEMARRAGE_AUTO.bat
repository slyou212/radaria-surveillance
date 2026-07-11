@echo off
REM =====================================================
REM  RadarIA — Demarrage automatique au boot Windows
REM  Place ce raccourci dans :
REM  C:\Users\%USERNAME%\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\
REM =====================================================
title RadarIA — Auto-demarrage

REM Attendre que le reseau soit disponible (30s)
echo Attente connexion reseau...
timeout /t 30 /nobreak >nul

REM Lancer la surveillance en arriere-plan
start "RadarIA Surveillance" /min "%~dp0LANCER_SURVEILLANCE.bat"
