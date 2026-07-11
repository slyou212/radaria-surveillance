@echo off
title RadarIA — Generateur package PC client
color 0B
echo.
echo  =====================================================
echo    RadarIA — Creation package nouveau PC client
echo    Lance ce script pour chaque nouveau client
echo  =====================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  ERREUR : Python n'est pas installe !
    pause
    exit /b 1
)

python "%~dp0creer_package.py"

echo.
echo  Le package se trouve dans : %~dp0packages_generes\
echo.
explorer "%~dp0packages_generes"
pause
