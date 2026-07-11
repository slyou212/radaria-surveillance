@echo off
title Corriger license_key — Gardien RadarIA
color 0B
echo.
echo  =====================================================
echo    Corriger la license_key du Gardien RadarIA
echo  =====================================================
echo.

REM Trouver config.json de RadarIA_PC
set CFG_SURV=%~dp0RadarIA_PC\config.json
set CFG_GARD=%~dp0gardien_config.json

echo  Config surveillance : %CFG_SURV%
echo  Config gardien      : %CFG_GARD%
echo.

if exist "%CFG_SURV%" (
    echo  Contenu actuel de RadarIA_PC\config.json :
    type "%CFG_SURV%" | findstr /i "license_key nom_magasin backoffice"
) else (
    echo  [ATTENTION] RadarIA_PC\config.json non trouve !
)
echo.

set /p LKEY="  Entre la license_key (visible dans backoffice.radaria.fr → client Slidis Market) : "

if "%LKEY%"=="" (
    echo  [ANNULE] Aucune license_key entree.
    pause & exit /b 1
)

REM Mise a jour dans RadarIA_PC\config.json
if exist "%CFG_SURV%" (
    python -c "import json; p=r'%CFG_SURV%'; c=json.load(open(p,encoding='utf-8')); c['license_key']='%LKEY%'; json.dump(c,open(p,'w',encoding='utf-8'),indent=2,ensure_ascii=False); print('  license_key mise a jour dans RadarIA_PC/config.json')"
) else (
    echo  [ATTENTION] RadarIA_PC\config.json absent, creation de gardien_config.json uniquement
)

REM Mise a jour dans gardien_config.json (fallback)
python -c "import json,os; p=r'%CFG_GARD%'; c=json.load(open(p,encoding='utf-8')) if os.path.exists(p) else {}; c['license_key']='%LKEY%'; c['backoffice_url']='https://backoffice.radaria.fr'; json.dump(c,open(p,'w',encoding='utf-8'),indent=2,ensure_ascii=False); print('  license_key mise a jour dans gardien_config.json')"

echo.
echo  [OK] license_key configuree. Lance maintenant LANCER_GARDIEN.bat
echo.
pause
