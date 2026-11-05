#!/usr/bin/env zsh
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=================================================================="
echo " Iniciando Avance Equipo 3 — Participation (FastAPI + React)"
echo "=================================================================="

# 1. Levantar Backend FastAPI en puerto 8000
PYTHONPATH="$ROOT_DIR/services/participation" \
  "$ROOT_DIR/services/participation/.venv/bin/uvicorn" app.main:app \
  --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# 2. Levantar Frontend React (Vite) en puerto 5173
npm --prefix "$ROOT_DIR/apps/participation-web" run dev -- --host 0.0.0.0 --port 5173 &
FRONTEND_PID=$!

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true" EXIT INT TERM

echo ""
echo " -> Participation API (Swagger): http://localhost:8000/docs"
echo " -> JWKS (Claves Públicas QR):   http://localhost:8000/.well-known/jwks.json"
echo " -> Interfaz Web React (Demo):   http://localhost:5173"
echo "=================================================================="
wait
