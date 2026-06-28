@echo off
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /r "Adresse IPv4"') do set IP=%%a
set IP=%IP: =%
echo.
echo ============================================
echo  SURVEILLANCE MAGASIN DEMARREE
echo  Dashboard : http://%IP%:5000
echo  Partagez cette adresse avec vos employes!
echo  (sur le meme reseau WiFi)
echo ============================================
echo.
cd /d "%~dp0"
python surveillance.py
pause
