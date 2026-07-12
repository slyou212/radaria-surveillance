@echo off
title RadarIA — Correction demarrage automatique
color 0E
echo.
echo  =====================================================
echo    RadarIA — Correction demarrage automatique
echo    Corrige : tache nuit + startup au boot
echo  =====================================================
echo.

REM Dossier de ce script (la ou tous les fichiers RadarIA sont)
set ROOT_DIR=%~dp0
set BAT_RESTART=%ROOT_DIR%RESTART_NUIT.bat
set BAT_DEMARRAGE=%ROOT_DIR%DEMARRAGE_AUTO.bat
set SURV_PY=%ROOT_DIR%RadarIA_PC\surveillance.py

echo  Dossier RadarIA detecte : %ROOT_DIR%
echo.

REM --- 1. Verifier que les fichiers existent ---
if not exist "%BAT_RESTART%" (
    color 0C
    echo  ERREUR : RESTART_NUIT.bat introuvable dans %ROOT_DIR%
    echo  Verifiez que vous lancez ce script depuis le bon dossier.
    pause & exit /b 1
)
if not exist "%SURV_PY%" (
    color 0C
    echo  ERREUR : RadarIA_PC\surveillance.py introuvable.
    pause & exit /b 1
)
echo  [OK] Fichiers trouves.
echo.

REM --- 2. Supprimer les anciennes taches planifiees (evite les doublons) ---
echo  [1/4] Suppression anciennes taches planifiees...
schtasks /delete /tn "RadarIA_Restart_Nuit" /f >nul 2>&1
schtasks /delete /tn "RadarIA_Demarrage_Auto" >nul 2>&1
echo  [OK] Anciennes taches supprimees.
echo.

REM --- 3. Recreer la tache nuit avec le bon chemin ---
echo  [2/4] Creation tache nuit 3h00 (RESTART_NUIT.bat)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%BAT_RESTART%'; $t = New-ScheduledTaskTrigger -Daily -At '03:00'; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10); Register-ScheduledTask -TaskName 'RadarIA_Restart_Nuit' -Action $a -Trigger $t -Settings $s -RunLevel Limited -Force | Out-Null; Write-Host '  Tache RadarIA_Restart_Nuit creee : 3h00 chaque nuit'"
echo.

REM --- 4. Installer DEMARRAGE_AUTO.bat dans le dossier Startup ---
echo  [3/4] Installation demarrage automatique au boot...
set STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
copy /Y "%BAT_DEMARRAGE%" "%STARTUP_FOLDER%\RadarIA_Demarrage.bat" >nul
if errorlevel 1 (
    echo  Startup folder inaccessible, creation tache au login...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$a = New-ScheduledTaskAction -Execute '%BAT_DEMARRAGE%'; $t = New-ScheduledTaskTrigger -AtLogOn; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -Delay '00:00:45'; Register-ScheduledTask -TaskName 'RadarIA_Demarrage_Auto' -Action $a -Trigger $t -Settings $s -RunLevel Limited -Force | Out-Null; Write-Host '  Tache RadarIA_Demarrage_Auto creee : au login'"
) else (
    echo  [OK] DEMARRAGE_AUTO.bat copie dans Startup.
    echo       Chemin : %STARTUP_FOLDER%\RadarIA_Demarrage.bat
)
echo.

REM --- 5. Relancer surveillance maintenant si elle ne tourne pas ---
echo  [4/4] Verification surveillance en cours...
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
if errorlevel 1 (
    echo  Surveillance non active — lancement immediat...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Start-Process -FilePath 'python' -ArgumentList '%SURV_PY%' -WorkingDirectory '%ROOT_DIR%RadarIA_PC' -WindowStyle Minimized"
    echo  [OK] Surveillance relancee en arriere-plan.
) else (
    echo  [OK] Surveillance deja en cours d'execution.
)
echo.

echo  =====================================================
echo    CORRECTION TERMINEE
echo.
echo    - Tache nuit 3h00 : CORRIGEE
echo    - Demarrage auto boot : INSTALLE
echo    - Surveillance : EN COURS
echo.
echo    A partir de maintenant :
echo    - Apres chaque reboot Windows -> RadarIA se lance seul
echo    - Chaque nuit a 3h00 -> surveillance.py se met a jour
echo  =====================================================
echo.
pause
