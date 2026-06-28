@echo off
echo ============================================
echo  INSTALLATION SURVEILLANCE MAGASIN
echo ============================================
echo.
python --version >nul 2>&1
if errorlevel 1 (
    echo ERREUR: Python n'est pas installe!
    echo Telechargez Python sur https://python.org
    echo Cochez "Add Python to PATH" lors de l'installation
    pause
    exit /b 1
)
echo Installation des modules Python...
pip install -r "%~dp0requirements.txt"
echo.
echo ============================================
echo  Installation terminee avec succes!
echo  Lancez maintenant 2_DEMARRER.bat
echo ============================================
pause
