# KenBot backend — start script (PowerShell)
# Usage:  .\start.ps1
# Optional overrides:
#   .\start.ps1 -PilotModel "anthropic/claude-3-5-sonnet"
#   .\start.ps1 -SurveyorModel "openai/gpt-4o"

param(
    [string]$PilotModel    = "",
    [string]$SurveyorModel = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = "C:\Users\nyaga\Documents\.venv"
$Python = "$Venv\Scripts\python.exe"

# Activate venv in this session
& "$Venv\Scripts\Activate.ps1" -ErrorAction SilentlyContinue

Set-Location $ScriptDir

# Apply optional model overrides
if ($PilotModel)    { $env:KENBOT_PILOT_MODEL    = $PilotModel }
if ($SurveyorModel) { $env:KENBOT_SURVEYOR_MODEL = $SurveyorModel }

# .env is auto-loaded by Django settings — nothing manual needed

# Verify token is present
$token = & $Python -c @"
import sys, os
sys.path.insert(0, '.')
os.environ.setdefault('DJANGO_SETTINGS_MODULE','kenbot.settings.development')
# load .env manually here for the check
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path('.env'), override=False)
except Exception:
    pass
token = os.environ.get('GITHUB_TOKEN','').strip()
if not token:
    home_token = Path.home() / '.kenbot' / 'github_token'
    if home_token.exists():
        token = home_token.read_text().strip()
if token:
    print('ok')
else:
    print('missing')
"@

if ($token -eq "missing") {
    Write-Host ""
    Write-Host "[!] GitHub token not found. Run the auth script first:" -ForegroundColor Yellow
    Write-Host "    cd .."
    Write-Host "    python auth_github.py"
    Write-Host ""
    exit 1
}

Write-Host ""
Write-Host "Starting KenBot backend..." -ForegroundColor Cyan
$pilotDisplay    = if ($env:KENBOT_PILOT_MODEL)    { $env:KENBOT_PILOT_MODEL }    else { 'openai/gpt-4o-mini (default)' }
$surveyorDisplay = if ($env:KENBOT_SURVEYOR_MODEL) { $env:KENBOT_SURVEYOR_MODEL } else { 'openai/gpt-4o (default)' }
Write-Host "  Pilot model:    $pilotDisplay"
Write-Host "  Surveyor model: $surveyorDisplay"
Write-Host "  WebSocket:      ws://localhost:8000/ws/pilot/<session_id>/"
Write-Host "  API:            http://localhost:8000/api/"
Write-Host ""

& $Python -m uvicorn kenbot.asgi:application --host 127.0.0.1 --port 8000 --app-dir $ScriptDir
