# KenBot - start all services
# Usage:  .\start-all.ps1
# Run from the repo root (kenbot/)

param(
    [string]$PilotModel    = "",
    [string]$SurveyorModel = ""
)

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv     = "C:\Users\nyaga\Documents\.venv"

# ---- 1. Redis ---------------------------------------------------------------
Write-Host ""
Write-Host "== 1/3  Redis ==" -ForegroundColor Cyan

$dockerOk = $null -ne (Get-Command docker -ErrorAction SilentlyContinue)
$redisOk  = $null -ne (Get-Command redis-server -ErrorAction SilentlyContinue)
$noRedis  = $false

if ($dockerOk) {
    $fmt = "{{.Names}}"
    $existing = & docker ps --filter "name=kenbot-redis" --format $fmt 2>&1
    if ($existing -match "kenbot-redis") {
        Write-Host "  Redis already running (Docker container kenbot-redis)" -ForegroundColor Green
    } else {
        Write-Host "  Starting Redis via Docker..."
        & docker run -d --name kenbot-redis -p 6379:6379 redis:7 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            & docker start kenbot-redis 2>&1 | Out-Null
        }
        Write-Host "  Redis started (Docker)" -ForegroundColor Green
    }
} elseif ($redisOk) {
    Write-Host "  Starting local redis-server..."
    Start-Process -FilePath "redis-server" -WindowStyle Minimized
    Write-Host "  Redis started (local)" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  [!] Redis not found. Install one of:" -ForegroundColor Yellow
    Write-Host "      Docker:  https://docs.docker.com/desktop/install/windows-install/"
    Write-Host "      Redis:   winget install Redis.Redis"
    Write-Host ""
    Write-Host "  Continuing without Redis - Celery will not start." -ForegroundColor Yellow
    Write-Host "  (Dev mode: CELERY_TASK_ALWAYS_EAGER runs tasks inline.)"
    Write-Host ""
    $noRedis = $true
}

Start-Sleep -Milliseconds 800

# ---- 2. Celery Worker -------------------------------------------------------
Write-Host ""
if ($noRedis) {
    Write-Host "== 2/3  Celery Worker - skipped (no Redis) ==" -ForegroundColor Yellow
} else {
    Write-Host "== 2/3  Celery Worker ==" -ForegroundColor Cyan
    Write-Host "  Starting in new window..."

    $workerScript = "Set-Location '$RepoRoot\backend'; & '$Venv\Scripts\Activate.ps1'; celery -A kenbot worker -l info"
    Start-Process powershell.exe -ArgumentList "-NoExit", "-Command", $workerScript -WorkingDirectory "$RepoRoot\backend"
    Write-Host "  Celery worker window opened" -ForegroundColor Green
}

Start-Sleep -Milliseconds 500

# ---- 3. Django / Daphne -----------------------------------------------------
Write-Host ""
Write-Host "== 3/3  Django (Daphne) ==" -ForegroundColor Cyan
Write-Host "  Starting Django in this window (Ctrl+C to stop)..."
Write-Host ""

& "$RepoRoot\backend\start.ps1" @PSBoundParameters
