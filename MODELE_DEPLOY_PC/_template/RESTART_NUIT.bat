@echo off
REM ============================================================
REM  RESTART NUIT — RadarIA
REM  Lance automatiquement par le Planificateur de taches a 3h00
REM  - Arrete surveillance.py
REM  - Telecharge la derniere version depuis le backoffice
REM  - Relance surveillance.py
REM ============================================================

set SURV_DIR=%~dp0..\RadarIA_PC
set SURV_PATH=%~dp0..\RadarIA_PC\surveillance.py
set BACKOFFICE=https://backoffice.radaria.fr
set LICENSE={{LICENSE_KEY}}
set LOG=%~dp0..\RadarIA_PC\restart_log.txt

echo [%date% %time%] === RESTART NUIT DEBUT === >> %LOG%

REM --- 1. Arreter le process Python ---
echo [%date% %time%] Arret du process Python... >> %LOG%
taskkill /F /IM python.exe /T >nul 2>&1
timeout /t 3 /nobreak >nul

REM --- 2. Telecharger la derniere version de surveillance.py ---
echo [%date% %time%] Telechargement surveillance.py depuis backoffice... >> %LOG%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { $url = '%BACKOFFICE%/api/update/surveillance/%LICENSE%'; $dest = '%SURV_PATH%'; (New-Object Net.WebClient).DownloadFile($url, $dest); Write-Output 'Telechargement OK' } catch { Write-Output 'Backoffice indisponible - version locale conservee' }" >> %LOG% 2>&1

REM --- 3. Relancer surveillance.py ---
echo [%date% %time%] Relance surveillance.py... >> %LOG%
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'python' -ArgumentList '%SURV_PATH%' -WorkingDirectory '%SURV_DIR%' -WindowStyle Minimized" >> %LOG% 2>&1

echo [%date% %time%] === RESTART NUIT TERMINE === >> %LOG%
echo. >> %LOG%
