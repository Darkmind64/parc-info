@echo off
REM merge_uploads_sync.cmd — Script launcher pour Windows
REM Détecte PowerShell et lance le script PS1

setlocal enabledelayedexpansion

echo.
echo ========================================================
echo   Merge Automation Launcher
echo ========================================================
echo.

REM Vérifier que PowerShell est disponible
powershell -Command "exit" 2>nul
if errorlevel 1 (
    echo ERROR: PowerShell not found
    exit /b 1
)

REM Lancer le script PowerShell
echo Lancement du script de merge...
echo.

powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0merge_uploads_sync.ps1"

if errorlevel 1 (
    echo.
    echo ERROR: Merge script failed
    exit /b 1
)

echo.
echo ========================================================
echo   Merge terminé!
echo ========================================================
echo.

pause
exit /b 0
