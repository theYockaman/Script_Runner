<#
Install script for Windows PowerShell.
Creates a virtual environment, installs requirements, and prepares folders.
#>
param()

Set-StrictMode -Version Latest

$root = Split-Path -Path $MyInvocation.MyCommand.Path -Parent
$venv = Join-Path $root '.venv'

Write-Host "Script Runner installer in $root"

function Ensure-Python {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) {
        Write-Error "Python is not on PATH. Please install Python 3.8+ and re-run this script."
        exit 1
    }
    & python --version
}

Ensure-Python

if (-not (Test-Path $venv)) {
    Write-Host "Creating virtual environment at $venv"
    python -m venv $venv
}

Write-Host "Activating virtual environment and installing requirements"
& "$venv\Scripts\Activate.ps1"
python -m pip install --upgrade pip
if (Test-Path "requirements.txt") {
    python -m pip install -r requirements.txt
} else {
    Write-Warning "requirements.txt not found; skipping pip install"
}

Write-Host "Creating data and scripts folders"
New-Item -ItemType Directory -Path (Join-Path $root 'data') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $root 'scripts') -Force | Out-Null

Write-Host "Done. Quick start:" 
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  uvicorn app.main:app --reload --host 0.0.0.0 --port 8080"

if (Get-Command docker -ErrorAction SilentlyContinue) {
    $ans = Read-Host "Docker detected. Build and run docker compose now? (y/N)"
    if ($ans -match '^[Yy]') {
        docker compose build
        docker compose up -d
    }
}

exit 0
