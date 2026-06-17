param(
  [switch]$NoBuild,
  [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
$serverDir = Join-Path $repoRoot 'server'
$logDir = Join-Path $repoRoot 'logs'
$desktopOut = Join-Path $logDir 'local-desktop.out.log'
$desktopErr = Join-Path $logDir 'local-desktop.err.log'
$devServerUrl = 'http://127.0.0.1:7777/'

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

function Wait-ElectronStarted($repoRoot, $timeoutSeconds) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  do {
    $process = Get-CimInstance Win32_Process -Filter "name = 'electron.exe'" -ErrorAction SilentlyContinue |
      Where-Object {
        $_.CommandLine -and
        $_.CommandLine.IndexOf($repoRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
      } |
      Select-Object -First 1

    if ($process) {
      return $true
    }

    Start-Sleep -Seconds 2
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

Write-Host "Stopping existing local desktop dev processes..."
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
Remove-Item -LiteralPath $desktopOut, $desktopErr -Force -ErrorAction SilentlyContinue

Write-Host "Starting desktop app with npm run dev..."
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
Remove-Item Env:VSCODE_DEBUG -ErrorAction SilentlyContinue
Remove-Item Env:VITE_DEV_SERVER_URL -ErrorAction SilentlyContinue
$env:VITE_BASE_URL = '/api'
$env:VITE_PROXY_URL = 'http://localhost:3001'
$env:VITE_USE_LOCAL_PROXY = 'true'
Start-Process `
  -FilePath 'npm.cmd' `
  -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1', '--port', '7777', '--strictPort') `
  -WorkingDirectory $repoRoot `
  -WindowStyle Hidden `
  -RedirectStandardOutput $desktopOut `
  -RedirectStandardError $desktopErr

if (-not (Wait-HttpOk $devServerUrl 60)) {
  Write-Host "Desktop stdout log: $desktopOut"
  Write-Host "Desktop stderr log: $desktopErr"
  throw "Desktop dev server did not become ready at $devServerUrl"
}

if (-not (Wait-ElectronStarted $repoRoot 90)) {
  Write-Host "Desktop stdout log: $desktopOut"
  Write-Host "Desktop stderr log: $desktopErr"
  throw "Electron desktop app did not start. Check the desktop logs above."
}

Write-Host ""
Write-Host "Local services are running:"
Write-Host "  Desktop: launched by npm run dev"
Write-Host "  Dev UI:  $devServerUrl"
Write-Host "  Backend: http://localhost:3001"
Write-Host "  API docs: http://localhost:3001/docs"
Write-Host ""
Write-Host "Logs:"
Write-Host "  $desktopOut"
Write-Host "  $desktopErr"

if ($OpenBrowser) {
  Write-Host ""
  Write-Host "OpenBrowser is ignored: this script starts the Electron desktop app."
}
