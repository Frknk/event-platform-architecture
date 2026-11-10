# start-demo.ps1
$ErrorActionPreference = "Continue"

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  Iniciando Plataforma de Eventos - Participation (FastAPI + React)" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

$RootDir = $PSScriptRoot
if (-not $RootDir) {
    $RootDir = (Get-Location).Path
}

# 1. Base de datos
Write-Host ""
Write-Host "[1/3] Verificando base de datos..." -ForegroundColor Yellow
$dockerRunning = $false
try {
    $null = & docker ps 2>&1
    if ($LASTEXITCODE -eq 0) {
        $dockerRunning = $true
    }
} catch {
    $dockerRunning = $false
}

if ($dockerRunning) {
    Write-Host "-> Docker detectado. Levantando PostgreSQL..." -ForegroundColor Green
    if (Get-Command "docker-compose" -ErrorAction SilentlyContinue) {
        & docker-compose up -d participation-db
    } else {
        & docker compose up -d participation-db
    }
} else {
    Write-Host "-> Docker no detectado. Usando base de datos SQLite preconfigurada." -ForegroundColor Green
}

# 2. Levantar Backend FastAPI
Write-Host ""
Write-Host "[2/3] Levantando Backend FastAPI en puerto 8000..." -ForegroundColor Yellow
$env:PYTHONPATH = "$RootDir\services\participation"
$backendProcess = Start-Process -FilePath "python" -ArgumentList "-m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload" -WorkingDirectory "$RootDir\services\participation" -PassThru

Start-Sleep -Seconds 2

# 3. Levantar Frontend Vite
Write-Host ""
Write-Host "[3/3] Levantando Frontend React Vite en puerto 5173..." -ForegroundColor Yellow
Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host "  Servicios listos:" -ForegroundColor Green
Write-Host "  -> Frontend:    http://localhost:5173" -ForegroundColor White
Write-Host "  -> Swagger API: http://localhost:8000/docs" -ForegroundColor White
Write-Host "==================================================================" -ForegroundColor Green
Write-Host "Presiona Ctrl+C en esta terminal para detener ambos servidores." -ForegroundColor Gray
Write-Host ""

try {
    Start-Process "http://localhost:5173" -ErrorAction SilentlyContinue
} catch {}

try {
    Push-Location "$RootDir\apps\participation-web"
    if (Get-Command "npm.cmd" -ErrorAction SilentlyContinue) {
        & npm.cmd run dev
    } else {
        & npm run dev
    }
} finally {
    Pop-Location
    if ($backendProcess -and -not $backendProcess.HasExited) {
        Write-Host ""
        Write-Host "Deteniendo Backend FastAPI..." -ForegroundColor Yellow
        Stop-Process -Id $backendProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
