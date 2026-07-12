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

set ROOT_DIR=%~dp0
set GARDIEN=%ROOT_DIR%gardien_radaria.py
set SURV=%ROOT_DIR%RadarIA_PC\surveillance.py

REM ── Creer/renouveler la tache planifiee gardien (toutes les 5 min) ─────────
echo [RadarIA] Installation tache planifiee gardien (5 min)...
schtasks /Delete /TN "RadarIA_Gardien" /F >nul 2>&1
schtasks /Create /TN "RadarIA_Gardien" ^
  /TR "python \"%GARDIEN%\"" ^
  /SC MINUTE /MO 5 /RL LIMITED /F /ST 00:00 >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$a = New-ScheduledTaskAction -Execute 'python' -Argument '\"%GARDIEN%\"' -WorkingDirectory '%ROOT_DIR%';" ^
      "$t = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) -Once -At (Get-Date);" ^
      "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew;" ^
      "Register-ScheduledTask -TaskName 'RadarIA_Gardien' -Action $a -Trigger $t -Settings $s -RunLevel Limited -Force | Out-Null"
)
echo [RadarIA] Tache gardien OK (toutes les 5 minutes).

REM ── Lancer la surveillance en arriere-plan ────────────────────────────────
echo [RadarIA] Lancement surveillance...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'python' -ArgumentList '\"%SURV%\"' -WorkingDirectory '%ROOT_DIR%RadarIA_PC' -WindowStyle Minimized"

REM ── Premier passage du gardien ────────────────────────────────────────────
timeout /t 10 /nobreak >nul
echo [RadarIA] Premier passage du gardien...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'python' -ArgumentList '\"%GARDIEN%\"' -WorkingDirectory '%ROOT_DIR%' -WindowStyle Minimized"

echo [RadarIA] Systeme RadarIA lance avec succes.
