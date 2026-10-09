# Disposable Local DB Reset Script (PowerShell)
$ErrorActionPreference = "Continue"

# Auto-detect Docker Desktop if not already in session PATH
$dockerPaths = @(
    "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin",
    "$env:ProgramFiles\Docker\Docker\resources\bin",
    "C:\Program Files\Docker\Docker\resources\bin"
)
foreach ($dp in $dockerPaths) {
    if ((Test-Path $dp) -and ($env:Path -notlike "*$dp*")) {
        $env:Path = "$dp;$env:Path"
    }
}

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Host "?? [Disposable DB] Checking local database environment..." -ForegroundColor Cyan

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "??  [Disposable DB] Docker is not installed or not in PATH. Skipping local DB reset." -ForegroundColor Yellow
    exit 0
}

$dockerInfo = docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "??  [Disposable DB] Docker daemon is not running. Skipping local DB reset." -ForegroundColor Yellow
    exit 0
}

# Resolve target DB URL: environment variable overrides .env
$dbUrl = ""
if (Test-Path ".env") {
    $envMatch = Select-String -Path ".env" -Pattern "^DATABASE_URL=(.*)"
    if ($envMatch) {
        $dbUrl = $envMatch.Matches[0].Groups[1].Value.Trim("`"'")
    }
}
if (Test-Path ".env.local") {
    $envLocalMatch = Select-String -Path ".env.local" -Pattern "^DATABASE_URL=(.*)"
    if ($envLocalMatch) {
        $dbUrl = $envLocalMatch.Matches[0].Groups[1].Value.Trim("`"'")
    }
}
if ($env:DATABASE_URL) {
    $dbUrl = $env:DATABASE_URL
}

if ($dbUrl -and ($dbUrl -notmatch "localhost") -and ($dbUrl -notmatch "127.0.0.1") -and ($dbUrl -notmatch "host.docker.internal")) {
    Write-Host "?? [SAFETY GUARD] DATABASE_URL points to a remote/RDS host! Refusing to reset." -ForegroundColor Red
    Write-Host "Target: $dbUrl"
    exit 0
}

Write-Host "???  [Disposable DB] Tearing down previous local PostgreSQL container & volumes..." -ForegroundColor Yellow
docker compose down -v 2>&1 | Out-Null

Write-Host "?? [Disposable DB] Starting clean local PostgreSQL container..." -ForegroundColor Green
docker compose up -d db 2>&1 | Out-Null

Write-Host "? [Disposable DB] Waiting for PostgreSQL to be ready..." -ForegroundColor Cyan
$retries = 30
while ($retries -gt 0) {
    $ready = docker compose exec -T db pg_isready -U postgres 2>&1
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 1
    $retries--
}

if ($retries -eq 0) {
    Write-Host "? [Disposable DB] PostgreSQL failed to become ready in time." -ForegroundColor Red
    exit 1
}

Write-Host "?? [Disposable DB] Applying database schemas & master data..." -ForegroundColor Cyan
if (Test-Path "venv\Scripts\python.exe") {
    & "venv\Scripts\python.exe" -m postgresql_db.setup_pbac_schema
    & "venv\Scripts\python.exe" -m postgresql_db.cross_service_schema
    & "venv\Scripts\python.exe" -m postgresql_db.notifications_schema
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python -m postgresql_db.setup_pbac_schema
    python -m postgresql_db.cross_service_schema
    python -m postgresql_db.notifications_schema
}

Write-Host "? [Disposable DB] Local database successfully recreated and schemas applied!" -ForegroundColor Green