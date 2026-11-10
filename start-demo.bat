@echo off
chcp 65001 >nul
title Plataforma de Eventos — Launcher
cls
echo ==================================================================
echo   Iniciando Plataforma de Eventos — Participation (FastAPI + React)
echo ==================================================================
echo.

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

:: Verificar si Docker está activo para PostgreSQL (opcional)
docker ps >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [1/3] Docker detectado. Levantando contenedor PostgreSQL en puerto 5434...
    docker-compose up -d participation-db >nul 2>&1 || docker compose up -d participation-db >nul 2>&1
) else (
    echo [1/3] Docker Desktop no detectado. El servicio usara la base SQLite preconfigurada.
)

echo [2/3] Levantando Backend FastAPI (puerto 8000)...
start "FastAPI Backend (Puerto 8000)" cmd /k "title FastAPI Backend && cd /d "%ROOT%\services\participation" && set "PYTHONPATH=%ROOT%\services\participation" && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

ping 127.0.0.1 -n 3 >nul

echo [3/3] Levantando Frontend React Vite (puerto 5173)...
start "React Frontend (Puerto 5173)" cmd /k "title React Frontend && cd /d "%ROOT%\apps\participation-web" && npm run dev"

ping 127.0.0.1 -n 3 >nul

echo Abriendo navegador en http://localhost:5173 ...
start http://localhost:5173

echo.
echo ==================================================================
echo   Servidores en ejecucion:
echo   - Frontend:    http://localhost:5173
echo   - Swagger API: http://localhost:8000/docs
echo   - JWKS:        http://localhost:8000/.well-known/jwks.json
echo ==================================================================
echo.
echo Las ventanas del Backend y Frontend permaneceran abiertas para mostrar los logs.
echo Puedes presionar cualquier tecla para cerrar esta ventana auxiliar.
pause >nul
