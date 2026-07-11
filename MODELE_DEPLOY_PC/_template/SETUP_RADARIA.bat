@echo off
title RadarIA — Setup Nouveau PC
color 0B
echo.
echo  =====================================================
echo    RadarIA — Configuration Nouveau PC Client
echo  =====================================================
echo.

REM Verifier Python
python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  ERREUR : Python n'est pas installe !
    echo.
    echo  Telechargez Python 3.11 sur https://python.org/downloads
    echo  Cochez "Add Python to PATH" lors de l'installation.
    echo.
    pause
    exit /b 1
)

echo  [1/4] Installation des dependances Python...
echo.
pip install opencv-python requests psutil --quiet --no-warn-script-location
if errorlevel 1 (
    echo  Erreur installation pip. Tentative sans cache...
    pip install opencv-python requests psutil --no-cache-dir --quiet
)
echo  Dependances OK.
echo.

echo  [2/4] Verification license_key...
echo.
set /p LKEY="  Entrez la license_key (ou Entree si deja dans config.json) : "
if not "%LKEY%"=="" (
    echo  Mise a jour config.json...
    python -c "import json,sys; p=r'%~dp0config.json'; c=json.load(open(p,encoding='utf-8')); c['license_key']='%LKEY%'; json.dump(c,open(p,'w',encoding='utf-8'),indent=2,ensure_ascii=False); print('  license_key enregistree.')"
)
echo.

echo  [3/5] Detection reseau et cameras...
echo.
python "%~dp0verifier_systeme.py"
if errorlevel 1 (
    color 0E
    echo.
    echo  Verification terminee avec des avertissements.
    echo  Consultez le rapport.html qui s'est ouvert.
)
echo.

echo  [4/5] Configuration redemarrage automatique nuit (3h00)...
echo.
set BAT_RESTART=%~dp0..\RESTART_NUIT.bat
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%BAT_RESTART%'; $t = New-ScheduledTaskTrigger -Daily -At '03:00'; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5); Register-ScheduledTask -TaskName 'RadarIA_Restart_Nuit' -Action $a -Trigger $t -Settings $s -RunLevel Limited -Force | Out-Null; Write-Host '  Tache planifiee OK - Redemarrage chaque nuit a 3h00'"
echo.

echo  [5/5] Setup termine !
echo.
echo  =======================================================
echo    Le rapport s'est ouvert dans votre navigateur.
echo    Verifiez que tout est vert, puis lancez :
echo.
echo    LANCER_SURVEILLANCE.bat
echo.
echo    Le systeme se restartera automatiquement chaque nuit
echo    a 3h00 et telechargera les mises a jour automatiquement.
echo  =======================================================
echo.
pause
