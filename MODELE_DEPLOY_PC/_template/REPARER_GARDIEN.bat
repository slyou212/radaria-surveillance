@echo off
chcp 65001 >nul
title RadarIA — Reparation Gardien IA
color 0B
echo.
echo  ============================================================
echo    RadarIA -- Reparation complete du Gardien IA
echo    - Supprime l'ancienne tache si elle existe
echo    - Cree une tache planifiee toutes les 5 minutes
echo    - Lance le gardien immediatement
echo  ============================================================
echo.

set ROOT=%~dp0
set GARDIEN=%ROOT%gardien_radaria.py
set PYTHON=python

REM Verifier que gardien_radaria.py existe
if not exist "%GARDIEN%" (
    color 0C
    echo  ERREUR : gardien_radaria.py introuvable dans %ROOT%
    echo  Assurez-vous d'executer ce BAT depuis le bon dossier.
    pause
    exit /b 1
)
echo  [OK] gardien_radaria.py trouve : %GARDIEN%
echo.

echo  [1/4] Suppression ancienne tache planifiee...
schtasks /Delete /TN "RadarIA_Gardien" /F >nul 2>&1
schtasks /Delete /TN "RadarIA_Demarrage_Auto" /F >nul 2>&1
echo  OK (ou inexistante, c'est normal).
echo.

echo  [2/4] Creation tache planifiee toutes les 5 minutes...
schtasks /Create /TN "RadarIA_Gardien" ^
  /TR "\"%PYTHON%\" \"%GARDIEN%\"" ^
  /SC MINUTE /MO 5 ^
  /RL LIMITED ^
  /F ^
  /ST 00:00
if errorlevel 1 (
    echo  Tentative alternative via PowerShell...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$action  = New-ScheduledTaskAction -Execute '%PYTHON%' -Argument '\"%GARDIEN%\"' -WorkingDirectory '%ROOT%';" ^
      "$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 5) -Once -At (Get-Date);" ^
      "$settings= New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew;" ^
      "Register-ScheduledTask -TaskName 'RadarIA_Gardien' -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null;" ^
      "Write-Host '  Tache planifiee cree via PowerShell.'"
) else (
    echo  Tache planifiee cree (schtasks).
)
echo.

echo  [3/4] Verification de la tache...
schtasks /Query /TN "RadarIA_Gardien" /FO LIST 2>nul | findstr /i "TaskName Status Next Run"
echo.

echo  [4/4] Lancement immediat du gardien...
echo  (Une fenetre va s'ouvrir brievement, c'est normal)
start "" /WAIT "%PYTHON%" "%GARDIEN%"
echo.
echo  Premier passage du gardien termine.
echo.

echo  ============================================================
echo   SUCCES ! Le gardien tourne maintenant toutes les 5 minutes.
echo   Verifiez le backoffice dans 5-10 min : statut doit etre EN LIGNE.
echo.
echo   Log du gardien : %ROOT%gardien.log
echo  ============================================================
echo.
pause
