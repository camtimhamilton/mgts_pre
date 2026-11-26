param([int]$DashboardPort = 8000, [int]$MLPort = 8001, [switch]$Seed, [int]$RunSeconds = 0,
      [string]$MentorXlsx, [string]$InternXlsx,
      [ValidateSet('local_rules','local_encoder','deepseek')][string]$TextMode,
      [ValidateSet('expert','expert_v1','legacy')][string]$PairPolicy = 'expert')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logsPath = Join-Path $projectRoot 'data/integration'
New-Item -ItemType Directory -Force -Path $logsPath | Out-Null
$backendPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
$mlPython = Join-Path $projectRoot 'ml_service/.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $backendPython) -or !(Test-Path -LiteralPath $mlPython)) {
    throw 'Create both virtual environments first: integration/README.md'
}
$env:ANALYSIS_MODE = 'ml'
$env:LLM_MODE = 'fake'
$env:ML_API_URL = "http://127.0.0.1:$MLPort"
$env:ML_MAPPING_VERSION = 'dashboard-demo-1'
$env:ML_KNOWLEDGE_VERSION = '0.1.0'
$env:ML_PROGRAM_ID = 'synthetic_dashboard'
$env:PULSE_PAIR_POLICY = $PairPolicy
$env:DB_PATH = Join-Path $projectRoot 'data/pair-v2.db'
if ([bool]$MentorXlsx -ne [bool]$InternXlsx) { throw 'Supply both -MentorXlsx and -InternXlsx.' }
if ($MentorXlsx) {
    $mentorPath = (Resolve-Path -LiteralPath $MentorXlsx).Path
    $internPath = (Resolve-Path -LiteralPath $InternXlsx).Path
    Push-Location $projectRoot
    try {
        & $backendPython -m tools.import_pair_history --mentor $mentorPath --intern $internPath
        if ($LASTEXITCODE -ne 0) { throw 'History import failed.' }
    } finally { Pop-Location }
}
if ($TextMode) { $env:PULSE_TEXT_MODE = $TextMode }
elseif (!$env:PULSE_TEXT_MODE) { $env:PULSE_TEXT_MODE = 'local_rules' }
if ($env:PULSE_TEXT_MODE -eq 'deepseek' -and !$env:DEEPSEEK_API_KEY) {
    foreach ($file in @('ml_service/.env', '.env')) {
        $path = Join-Path $projectRoot $file
        if (!(Test-Path -LiteralPath $path)) { continue }
        $entry = Get-Content -LiteralPath $path -Encoding UTF8 | Select-String '^\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=\s*(\S.*?)\s*$' | Select-Object -Last 1
        if ($entry) {
            $env:DEEPSEEK_API_KEY = $entry.Matches[0].Groups[1].Value.Trim('"', "'")
            if ($env:DEEPSEEK_API_KEY) { break }
        }
    }
}
if (!$env:ML_API_TIMEOUT) { $env:ML_API_TIMEOUT = if ($env:PULSE_TEXT_MODE -eq 'deepseek') { '100' } else { '60' } }
$env:MGTS_PROVIDER = 'mock'
if ($env:PULSE_TEXT_MODE -eq 'deepseek') { $env:MGTS_ALLOW_SYNTHETIC_LLM = '1' }
else { $env:MGTS_ALLOW_SYNTHETIC_LLM = '0' }
$env:MGTS_DATABASE = Join-Path $logsPath 'ml.sqlite3'
$env:MGTS_MAPPING_PATH = Join-Path $projectRoot 'ml_service/mappings/dashboard-demo.yaml'
$env:MGTS_FIXTURE_CATALOG = Join-Path $projectRoot 'ml_service/tests/fixtures/dashboard_mock_extractions.json'
$processes = @()
try {
    $processes += Start-Process -FilePath $mlPython -ArgumentList @('-m','uvicorn','app.main:create_app','--factory','--host','127.0.0.1','--port',"$MLPort") -WorkingDirectory (Join-Path $projectRoot 'ml_service') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logsPath 'ml.out.log') -RedirectStandardError (Join-Path $logsPath 'ml.err.log')
    $processes += Start-Process -FilePath $backendPython -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$DashboardPort") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logsPath 'dashboard.out.log') -RedirectStandardError (Join-Path $logsPath 'dashboard.err.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($processes | Where-Object { $_.HasExited }) { throw "Service exited. See $logsPath" }
        try {
            $null = Invoke-RestMethod "http://127.0.0.1:$MLPort/health" -TimeoutSec 1
            $null = Invoke-RestMethod "http://127.0.0.1:$DashboardPort/api/health" -TimeoutSec 1
            $ready = $true
            break
        } catch { Start-Sleep -Milliseconds 250 }
    }
    if (!$ready) { throw "Startup timeout. See $logsPath" }
    if ($Seed) {
        $samples = Get-Content (Join-Path $PSScriptRoot 'synthetic_ingest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($sample in $samples) {
            $body = [System.Text.Encoding]::UTF8.GetBytes(($sample | ConvertTo-Json))
            $null = Invoke-RestMethod "http://127.0.0.1:$DashboardPort/api/ingest" -Method Post -ContentType 'application/json; charset=utf-8' -Body $body
        }
    }
    Write-Host "Dashboard http://127.0.0.1:$DashboardPort | ML http://127.0.0.1:$MLPort/docs"
    Write-Host "Pair policy: $env:PULSE_PAIR_POLICY | Text: $env:PULSE_TEXT_MODE | DB: $env:DB_PATH | Ctrl+C stops both services."
    $startedAt = Get-Date
    while ($RunSeconds -le 0 -or ((Get-Date) - $startedAt).TotalSeconds -lt $RunSeconds) {
        if ($processes | Where-Object { $_.HasExited }) { throw "Service exited. See $logsPath" }
        Start-Sleep -Milliseconds 500
    }
} finally {
    foreach ($process in $processes) {
        if (!$process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
}
