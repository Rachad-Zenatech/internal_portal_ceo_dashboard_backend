# Pull Production Data into Local Docker Database (Port 3001)
$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$dockerBin = "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin"
if ((Test-Path $dockerBin) -and ($env:Path -notlike "*$dockerBin*")) {
    $env:Path = "$dockerBin;$env:Path"
}

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " Pull Production Data -> Local Docker Database" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

$prodUrl = ""
if (Test-Path ".env") {
    $envMatch = Select-String -Path ".env" -Pattern "^DATABASE_URL=(.*)"
    if ($envMatch) {
        $prodUrl = $envMatch.Matches[0].Groups[1].Value.Trim(@('"', "'", "
", "
"))
    }
}

if (-not $prodUrl -or ($prodUrl -like "*localhost*") -or ($prodUrl -like "*127.0.0.1*")) {
    Write-Host "? Error: Could not find remote DATABASE_URL in .env" -ForegroundColor Red
    exit 1
}

Write-Host "Source DB    : $prodUrl" -ForegroundColor Gray
Write-Host "Target Local : ceo_db_local (localhost:3001 / zenatech_ceo_dashboard)" -ForegroundColor Green
Write-Host ""

Write-Host "[1/3] Ensuring local Docker database is running..." -ForegroundColor Cyan
docker compose up -d db | Out-Null

$retries = 15
while ($retries -gt 0) {
    docker exec ceo_db_local pg_isready -U postgres -d zenatech_ceo_dashboard 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 1
    $retries--
}

Write-Host "[2/3] Fetching live data dump from remote database..." -ForegroundColor Cyan
docker exec ceo_db_local sh -c "pg_dump '$prodUrl' --schema=public --no-owner --no-acl -Fc -f /tmp/prod.dump"

Write-Host "[3/3] Restoring live data into local database..." -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
$null = docker exec ceo_db_local sh -c "pg_restore -U postgres -d zenatech_ceo_dashboard --clean --if-exists --no-owner /tmp/prod.dump" 2>&1
$ErrorActionPreference = "Stop"
docker exec ceo_db_local rm -f /tmp/prod.dump

Write-Host ""
Write-Host "? Local Docker database successfully updated with live data!" -ForegroundColor Green
Write-Host "You can now inspect all real rows in DataGrip on localhost:3001." -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
