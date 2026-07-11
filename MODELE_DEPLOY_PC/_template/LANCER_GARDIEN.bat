@echo off
title Gardien RadarIA
color 0B
echo.
echo  =====================================================
echo    Gardien RadarIA — Agent autonome
echo  =====================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  ERREUR : Python non installe.
    pause & exit /b 1
)

REM Premier argument : mode
if "%1"=="--install"    goto install
if "%1"=="--surveille"  goto surveille
if "%1"=="--daemon"     goto daemon
if "%1"=="--diagnostic" goto diagnostic
goto auto

:install
echo  Mode : INSTALLATION
python "%~dp0gardien_radaria.py" --install
goto fin

:surveille
echo  Mode : SURVEILLANCE (1 cycle)
python "%~dp0gardien_radaria.py" --surveille
goto fin

:daemon
echo  Mode : DAEMON (tourne en permanence, Ctrl+C pour arreter)
python "%~dp0gardien_radaria.py" --daemon
goto fin

:diagnostic
echo  Mode : DIAGNOSTIC
python "%~dp0gardien_radaria.py" --diagnostic
goto fin

:auto
echo  Mode : AUTO-DETECTION
python "%~dp0gardien_radaria.py"

:fin
echo.
pause
