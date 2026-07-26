param(
  [int]$WebPort = 5275,
  [int]$AppPort = 8230,
  [int]$BrokerPort = 8240,
  [int]$HarnessPort = 8250,
  [ValidateSet('account', 'guest')]
  [string]$Mode = 'account'
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
$artifactTier = if ($Mode -eq 'guest') { 'guest' } else { 'local' }
$runRoot = Join-Path ([System.IO.Path]::GetTempPath()) "leaf-unified-$artifactTier-e2e-$runId"
$artifactRoot = Join-Path $repoRoot "artifacts\unified-surface-proof\$artifactTier\stack-$runId"
$launcher = $null

function Test-PortOpen([int]$Port) {
  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $pending = $client.ConnectAsync('127.0.0.1', $Port)
    return $pending.Wait(250) -and $client.Connected
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

function Wait-Json([string]$Url, [scriptblock]$Accept, [int]$Seconds = 60) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  do {
    try {
      $value = Invoke-RestMethod -Uri $Url -TimeoutSec 3
      if (& $Accept $value) { return $value }
    } catch { }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  throw "Timed out waiting for $Url"
}

foreach ($port in @($WebPort, $AppPort, $BrokerPort, $HarnessPort)) {
  if (Test-PortOpen $port) { throw "Port $port is already in use" }
}

New-Item -ItemType Directory -Path $runRoot, $artifactRoot | Out-Null
foreach ($name in @('drawings', 'guest-drawings', 'uploads', 'grants', 'tenants', 'tenant-git')) {
  New-Item -ItemType Directory -Path (Join-Path $runRoot $name) | Out-Null
}

$env:LEAF_SOURCE_SHA = (git -C $repoRoot rev-parse HEAD).Trim()
$env:LEAF_SOURCE_COMMIT = $env:LEAF_SOURCE_SHA
$env:LEAF_AUTH_LIVE = if ($Mode -eq 'guest') { '1' } else { '0' }
$env:LEAF_AGENT_MOCK = '1'
$env:LEAF_GUEST_UPLOADS_ENABLED = '1'
$env:LEAF_GUEST_SECRET = [Guid]::NewGuid().ToString('N')
$env:LEAF_CUSTOMIZATION_R5_MODE = 'off'
$env:LEAF_CUSTOMIZATION_R6_MODE = 'off'
$env:LEAF_STORE_DIR = Join-Path $runRoot 'drawings'
$env:LEAF_GUEST_STORE_DIR = Join-Path $runRoot 'guest-drawings'
$env:LEAF_UPLOADS_DIR = Join-Path $runRoot 'uploads'
$env:JOBS_DB = Join-Path $runRoot 'jobs.db'
$env:SESSIONS_DB = Join-Path $runRoot 'sessions.db'
$env:LEAF_AGENT_LEDGER = Join-Path $runRoot 'agent-ledger.jsonl'
$env:BROKER_LEDGER = Join-Path $runRoot 'broker-ledger.jsonl'
$env:BROKER_TENANTS = Join-Path $runRoot 'broker-tenants.json'
$env:LEAF_GRANTS_DIR = Join-Path $runRoot 'grants'
$env:LEAF_GRANT_FILE = Join-Path $runRoot 'no-legacy-grant.token'
$env:LEAF_OPS_SECRET = [Guid]::NewGuid().ToString('N')
$env:LEAF_E2E_OPS_SECRET = $env:LEAF_OPS_SECRET
$env:CLAUDE_CODE_OAUTH_TOKEN = ''
$env:ANTHROPIC_API_KEY = ''
$env:LEAF_TENANTS_DIR = Join-Path $runRoot 'tenants'
$env:LEAF_TENANT_GIT_DIR = Join-Path $runRoot 'tenant-git'
$env:LEAF_E2E_BASE_URL = "http://127.0.0.1:$WebPort"
$env:LEAF_E2E_API_BASE = "http://127.0.0.1:$AppPort"
$env:LEAF_E2E_MANAGED = '1'
$env:VITE_STARTUP_FETCH_TIMEOUT_MS = '15000'
$env:LEAF_CORS_ORIGINS = $env:LEAF_E2E_BASE_URL

$stdout = Join-Path $runRoot 'stack.out.log'
$stderr = Join-Path $runRoot 'stack.err.log'
$proofExitCode = 1

try {
  $managedVenv = Join-Path $runRoot 'python-runtime'
  uv venv $managedVenv --python 3.13 | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not create the managed proof Python runtime' }
  $launcherFile = Join-Path $managedVenv 'Scripts\python.exe'
  $requirementArgs = @(
    'pip', 'install', '--python', $launcherFile,
    '-r', (Join-Path $repoRoot 'server\requirements.txt'),
    '-r', (Join-Path $repoRoot 'platform\requirements.txt')
  )
  if ($Mode -eq 'guest') {
    $requirementArgs += @('-r', (Join-Path $repoRoot 'server\requirements-auth.txt'))
  }
  & uv @requirementArgs | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Could not install the managed proof Python dependencies' }
  $launcherArgs = @(
    'scripts/start-leaf.py', '--with-harness',
    '--broker-port', $BrokerPort,
    '--app-port', $AppPort,
    '--harness-port', $HarnessPort,
    '--web-port', $WebPort
  )
  $launcher = Start-Process -FilePath $launcherFile -ArgumentList $launcherArgs -WorkingDirectory $repoRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru

  Wait-Json "http://127.0.0.1:$BrokerPort/broker/health" { param($x) $x.ok -and $x.role -eq 'aps-broker' } | Out-Null
  Wait-Json "http://127.0.0.1:$HarnessPort/health" { param($x) $x.ok } | Out-Null
  $health = Wait-Json "http://127.0.0.1:$AppPort/api/health" { param($x) $x.ok -and -not $x.aps_live }
  if ($health.source_sha -ne $env:LEAF_SOURCE_SHA) { throw 'App source revision does not match the tested commit' }
  Wait-Json "http://127.0.0.1:$AppPort/api/ready" { param($x) $x.ready -and $x.dependencies.durable_stores.state -eq 'ready' } | Out-Null

  Push-Location (Join-Path $repoRoot 'web')
  try {
    if ($Mode -eq 'guest') { npm run proof:guest } else { npm run proof:local }
    $proofExitCode = $LASTEXITCODE
  } finally { Pop-Location }
} finally {
  if ($launcher -and -not $launcher.HasExited) {
    Stop-Process -Id $launcher.Id
    $launcher.WaitForExit(10000) | Out-Null
  }
  if (Test-Path -LiteralPath $stdout) { Copy-Item -LiteralPath $stdout -Destination $artifactRoot }
  if (Test-Path -LiteralPath $stderr) { Copy-Item -LiteralPath $stderr -Destination $artifactRoot }
  Write-Host "Local proof runtime retained at $runRoot"
  Write-Host "Redacted stack logs copied to $artifactRoot"
}

exit $proofExitCode
