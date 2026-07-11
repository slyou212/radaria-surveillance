@echo off
title RadarIA — Surveillance
color 0A
echo.
echo  =====================================================
echo    RadarIA — Lancement Surveillance
echo    Slidis Market
echo  =====================================================
echo.

REM Verifier config.json (une seule ligne pour compatibilite cmd.exe)
python -c "import json,sys; c=json.load(open(r'%~dp0..\RadarIA_PC\config.json',encoding='utf-8')); sys.exit(0) if c.get('license_key') else (print('ERREUR: license_key manquante'),sys.exit(1))" 2>nul || python -c "import json,sys; c=json.load(open(r'%~dp0config.json',encoding='utf-8')); print('Config OK -',c.get('nom_magasin','?'))" 2>nul
if errorlevel 1 (
    color 0C
    echo.
    echo  Configurez d'abord config.json avec la license_key !
    echo  Relancez SETUP_RADARIA.bat
    pause
    exit /b 1
)

echo.

REM Lancer la surveillance depuis RadarIA_PC (config.json deja present, ne pas ecraser)
set SURV_DIR=%~dp0..\RadarIA_PC
if exist "%SURV_DIR%\surveillance.py" (
    echo  Lancement de la surveillance...
    echo  (Ctrl+C pour arreter)
    echo.
    cd /d "%SURV_DIR%"
    python surveillance.py
) else (
    echo.
    echo  ATTENTION : surveillance.py non trouve dans RadarIA_PC\
    echo  Verifiez que le dossier RadarIA_PC est present.
    echo.
    echo  Structure attendue :
    echo    C:\radaria-clean\
    echo      NOUVEAU_PC\      ^<-- ce dossier
    echo      RadarIA_PC\      ^<-- surveillance.py doit etre ici
    echo.
    pause
)
