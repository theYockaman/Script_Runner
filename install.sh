#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

echo "Installing Script_Runner into: $ROOT_DIR"

# check python
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python is not installed or not on PATH. Please install Python 3.8+." >&2
  exit 1
fi

echo "Using Python: $($PY --version)"

# create venv
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment in $VENV_DIR"
  $PY -m venv "$VENV_DIR"
fi

echo "Activating virtual environment and installing requirements"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
if [ -f requirements.txt ]; then
  pip install -r requirements.txt
else
  echo "requirements.txt not found; skipping pip install"
fi

echo "Creating data and scripts folders"
mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/scripts"

echo "Making scripts executable"
if [ -d "$ROOT_DIR/scripts" ]; then
  chmod +x "$ROOT_DIR/scripts"/* 2>/dev/null || true
fi

echo "Done. Quick start:" 
echo "  source .venv/bin/activate"
echo "  uvicorn app.main:app --reload --host 0.0.0.0 --port 8080"

if command -v docker >/dev/null 2>&1; then
  read -r -p "Docker detected. Do you want to build and bring up containers with docker compose? [y/N] " ans
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    docker compose build
    docker compose up -d
  fi
fi

exit 0
