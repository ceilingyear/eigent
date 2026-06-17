param(
  [switch]$NoBuild,
  [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
$serverDir = Join-Path $repoRoot 'server'
$logDir = Join-Path $repoRoot 'logs'
$frontendOut = Join-Path $logDir 'local-frontend.out.log'
$frontendErr = Join-Path $logDir 'local-frontend.err.log'

function Assert-Command($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Required command '$name' was not found in PATH."
  }
}

function Set-EnvLine($path, $key, $value) {
  $line = "$key=$value"
  if (-not (Test-Path $path)) {
    Set-Content -LiteralPath $path -Value $line
    return
  }

  $content = Get-Content -LiteralPath $path
  $pattern = "^\s*#?\s*$([regex]::Escape($key))="
  $found = $false
  $updated = foreach ($existingLine in $content) {
    if ($existingLine -match $pattern) {
      $found = $true
      $line
    } else {
      $existingLine
    }
  }

  if (-not $found) {
    $updated += $line
  }

  Set-Content -LiteralPath $path -Value $updated
}

function Wait-HttpOk($url, $timeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  do {
    try {
      $res = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
      if ($res.StatusCode -ge 200 -and $res.StatusCode -lt 300) {
        return $true
      }
    } catch {
      Start-Sleep -Seconds 2
    }
  } while ((Get-Date) -lt $deadline)

  return $false
}

Assert-Command docker
Assert-Command npm

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$serverEnv = Join-Path $serverDir '.env'
$serverEnvExample = Join-Path $serverDir '.env.example'
if (-not (Test-Path $serverEnv)) {
  if (-not (Test-Path $serverEnvExample)) {
    throw "Missing server env template: $serverEnvExample"
  }
  Copy-Item -LiteralPath $serverEnvExample -Destination $serverEnv
  Write-Host "Created server/.env from server/.env.example"
}

$devEnv = Join-Path $repoRoot '.env.development'
Set-EnvLine $devEnv 'VITE_BASE_URL' '/api'
Set-EnvLine $devEnv 'VITE_PROXY_URL' 'http://localhost:3001'
Set-EnvLine $devEnv 'VITE_USE_LOCAL_PROXY' 'true'
Write-Host "Configured .env.development for local backend."

Write-Host "Starting Docker backend..."
Push-Location $serverDir
try {
  if ($NoBuild) {
    docker compose up -d
  } else {
    docker compose up --build -d
  }
} finally {
  Pop-Location
}

if (-not (Wait-HttpOk 'http://localhost:3001/health' 90)) {
  docker compose --project-directory $serverDir ps
  throw "Backend did not become healthy at http://localhost:3001/health"
}
Write-Host "Backend is ready: http://localhost:3001"

Write-Host "Stopping existing local frontend processes..."
Get-Process |
  Where-Object {
    ($_.ProcessName -eq 'electron' -and $_.Path -like "$repoRoot*") -or
    ($_.ProcessName -eq 'node' -and $_.Path -like "$repoRoot*")
  } |
  Stop-Process -Force -ErrorAction SilentlyContinue

Get-CimInstance Win32_Process -Filter "name = 'node.exe'" |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains($repoRoot) } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Remove-Item -LiteralPath (Join-Path $repoRoot 'node_modules\.vite') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $frontendOut, $frontendErr -Force -ErrorAction SilentlyContinue

Write-Host "Starting Vite frontend..."
$env:VSCODE_DEBUG = 'true'
$env:VITE_DEV_SERVER_URL = 'http://127.0.0.1:7777/'
Start-Process `
  -FilePath 'npm.cmd' `
  -ArgumentList @('run', 'dev') `
  -WorkingDirectory $repoRoot `
  -WindowStyle Hidden `
  -RedirectStandardOutput $frontendOut `
  -RedirectStandardError $frontendErr

if (-not (Wait-HttpOk 'http://127.0.0.1:7777/' 60)) {
  Write-Host "Frontend stdout log: $frontendOut"
  Write-Host "Frontend stderr log: $frontendErr"
  throw "Frontend did not become ready at http://127.0.0.1:7777/"
}

Write-Host ""
Write-Host "Local services are running:"
Write-Host "  Frontend: http://127.0.0.1:7777/"
Write-Host "  Backend:  http://localhost:3001"
Write-Host "  API docs: http://localhost:3001/docs"
Write-Host ""
Write-Host "Logs:"
Write-Host "  $frontendOut"
Write-Host "  $frontendErr"

if ($OpenBrowser) {
  Start-Process 'http://127.0.0.1:7777/'
}
