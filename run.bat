@echo off
title sot-cli - entorno conda (sot)
cd /d "%~dp0"

echo Activando entorno conda: sot
call C:\dev\miniconda3\condabin\conda.bat activate sot
if errorlevel 1 (
    echo ERROR: No se pudo activar el entorno sot
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Entorno 'sot' activado
echo   Proyecto: %CD%
echo ========================================
echo.
echo   Ejecucion manual:
echo     sot-cli --provider openrouter
echo     python -m sot_cli --provider openrouter
echo.
cmd /k
