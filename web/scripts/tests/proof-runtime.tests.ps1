$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\lib\proof-runtime.ps1')

$script:failures = 0
function Assert-Proof {
  param([bool]$Condition, [string]$Name)
  if ($Condition) { Write-Host "PASS $Name" }
  else { Write-Host "FAIL $Name"; $script:failures++ }
}

function Test-ProofCase {
  param([string]$Name, [scriptblock]$Body)
  try { & $Body } catch { Assert-Proof $false "$Name ($($_.Exception.Message))" }
}

function New-TestRuntime {
  param([string]$Root, [string]$Key, [datetime]$Stamp)
  $dir = Join-Path $Root $Key
  [System.IO.Directory]::CreateDirectory((Join-Path $dir 'Scripts')) | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $dir 'Scripts\python.exe'), '')
  $marker = Join-Path $dir '.leaf-proof-complete'
  [System.IO.File]::WriteAllText($marker, $Key)
  [System.IO.File]::SetLastWriteTime($marker, $Stamp)
  return $dir
}

$sandboxParent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\', '/')
$sandboxName = 'proof-runtime-test-' + [guid]::NewGuid().ToString('N')
$sandbox = Join-Path $sandboxParent $sandboxName
$savedLog = $env:LEAF_PROOF_TEST_UV_LOG
$savedFailure = $env:LEAF_PROOF_TEST_UV_FAIL
$heldLock = $null
try {
  $tempRoot = Join-Path $sandbox 'temp'
  $cacheRoot = Join-Path $sandbox 'cache'
  [System.IO.Directory]::CreateDirectory($tempRoot) | Out-Null
  [System.IO.Directory]::CreateDirectory($cacheRoot) | Out-Null
  $reqA = Join-Path $sandbox 'requirements.txt'
  $reqB = Join-Path $sandbox 'requirements-auth.txt'
  [System.IO.File]::WriteAllBytes($reqA, [byte[]]@(97, 10))
  [System.IO.File]::WriteAllBytes($reqB, [byte[]]@(98, 13, 10))
  $reqs = @($reqA, $reqB)
  $fakeUv = Join-Path $sandbox 'fake-uv.ps1'
  $env:LEAF_PROOF_TEST_UV_LOG = Join-Path $sandbox 'uv.log'
  $env:LEAF_PROOF_TEST_UV_FAIL = ''
  [System.IO.File]::WriteAllText($fakeUv, @'
$ErrorActionPreference = 'Stop'
[System.IO.File]::AppendAllText($env:LEAF_PROOF_TEST_UV_LOG, ($args -join ' ') + [Environment]::NewLine)
if ($args[0] -eq 'venv') {
  $dir = [string]$args[1]
  [System.IO.Directory]::CreateDirectory((Join-Path $dir 'Scripts')) | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $dir 'Scripts\python.exe'), '')
  if ($env:LEAF_PROOF_TEST_UV_FAIL -eq 'venv') { exit 1 }
  exit 0
}
if ($args[0] -eq 'pip' -and $args[1] -eq 'install') {
  if ($env:LEAF_PROOF_TEST_UV_FAIL -eq 'pip') { exit 1 }
  exit 0
}
exit 2
'@)

  Test-ProofCase 'requirements key' {
    $key = Get-ProofRuntimeKey -RequirementFiles $reqs -PythonVersion '3.13'
    Assert-Proof ($key -cmatch '^[a-f0-9]{16}$') 'key is 16 lowercase hex characters'
    Assert-Proof ($key -ceq (Get-ProofRuntimeKey -RequirementFiles $reqs -PythonVersion '3.13')) 'key is stable'
    [System.IO.File]::WriteAllBytes($reqA, [byte[]]@(97, 13))
    Assert-Proof ($key -cne (Get-ProofRuntimeKey -RequirementFiles $reqs -PythonVersion '3.13')) 'one byte changes the key'
    [System.IO.File]::WriteAllBytes($reqA, [byte[]]@(97, 10))
    Assert-Proof ($key -cne (Get-ProofRuntimeKey -RequirementFiles $reqs -PythonVersion '3.14')) 'Python version changes the key'
    Assert-Proof ($key -cne (Get-ProofRuntimeKey -RequirementFiles @($reqB, $reqA) -PythonVersion '3.13')) 'requirement order changes the key'
    $missingThrew = $false
    try { Get-ProofRuntimeKey -RequirementFiles (Join-Path $sandbox 'missing.txt') -PythonVersion '3.13' | Out-Null }
    catch { $missingThrew = $_.Exception.Message -like 'Missing proof runtime requirement file:*' }
    Assert-Proof $missingThrew 'missing requirement has a clear error'
  }

  Test-ProofCase 'build and reuse' {
    $python = Get-ProofRuntime -RequirementFiles $reqs -CacheRoot $cacheRoot -UvCommand $fakeUv
    $calls = [System.IO.File]::ReadAllLines($env:LEAF_PROOF_TEST_UV_LOG)
    Assert-Proof ($calls.Count -eq 2 -and $calls[0].StartsWith('venv ') -and $calls[1].StartsWith('pip install ')) 'first use builds then installs'
    $expectedPip = 'pip install --python ' + $python + ' -r ' + $reqA + ' -r ' + $reqB
    Assert-Proof ($calls[1] -ceq $expectedPip) 'install preserves the ordered requirement list'
    $runtime = Split-Path (Split-Path $python -Parent) -Parent
    $key = Split-Path $runtime -Leaf
    $marker = Join-Path $runtime '.leaf-proof-complete'
    Assert-Proof ([System.IO.File]::Exists($python) -and [System.IO.File]::ReadAllText($marker) -ceq $key) 'build writes the completion marker'
    $before = [datetime]::Now.AddHours(-2)
    [System.IO.File]::SetLastWriteTime($marker, $before)
    $reused = Get-ProofRuntime -RequirementFiles $reqs -CacheRoot $cacheRoot -UvCommand $fakeUv
    Assert-Proof ($reused -ceq $python -and [System.IO.File]::ReadAllLines($env:LEAF_PROOF_TEST_UV_LOG).Count -eq 2) 'second use returns the same path without uv'
    Assert-Proof ([System.IO.File]::GetLastWriteTime($marker) -gt $before) 'reuse advances the use stamp'
  }

  Test-ProofCase 'partial and failed builds' {
    $partialCache = Join-Path $sandbox 'partial-cache'
    $key = Get-ProofRuntimeKey -RequirementFiles $reqs -PythonVersion '3.13'
    $runtime = Join-Path $partialCache $key
    [System.IO.Directory]::CreateDirectory($runtime) | Out-Null
    $sentinel = Join-Path $runtime 'partial.txt'
    [System.IO.File]::WriteAllText($sentinel, 'partial')
    $beforeCalls = [System.IO.File]::ReadAllLines($env:LEAF_PROOF_TEST_UV_LOG).Count
    $python = Get-ProofRuntime -RequirementFiles $reqs -CacheRoot $partialCache -UvCommand $fakeUv
    Assert-Proof (-not [System.IO.File]::Exists($sentinel) -and [System.IO.File]::Exists($python)) 'unmarked partial runtime is replaced'
    Assert-Proof ([System.IO.File]::ReadAllLines($env:LEAF_PROOF_TEST_UV_LOG).Count -eq ($beforeCalls + 2)) 'partial runtime is rebuilt with uv'
    foreach ($failure in @('pip', 'venv')) {
      $failedCache = Join-Path $sandbox "$failure-failure-cache"
      $env:LEAF_PROOF_TEST_UV_FAIL = $failure
      $message = ''
      try { Get-ProofRuntime -RequirementFiles $reqs -CacheRoot $failedCache -UvCommand $fakeUv | Out-Null }
      catch { $message = $_.Exception.Message }
      finally { $env:LEAF_PROOF_TEST_UV_FAIL = '' }
      $expected = 'Could not install the managed proof Python dependencies'
      if ($failure -eq 'venv') { $expected = 'Could not create the managed proof Python runtime' }
      Assert-Proof ($message -ceq $expected) "$failure failure throws the runner error"
      Assert-Proof (-not [System.IO.Directory]::Exists((Join-Path $failedCache $key))) "$failure failure removes the partial runtime"
    }
    $lockPath = Join-Path $partialCache "$key.lock"
    $heldLock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try {
      $message = ''
      try { Get-ProofRuntime -RequirementFiles $reqs -CacheRoot $partialCache -UvCommand $fakeUv -LockTimeoutSeconds 0 | Out-Null }
      catch { $message = $_.Exception.Message }
      Assert-Proof ($message -ceq "Timed out waiting for proof runtime lock $lockPath") 'busy runtime lock times out without building'
    } finally { $heldLock.Dispose(); $heldLock = $null }
  }

  Test-ProofCase 'run root deletion boundaries' {
    $run = Join-Path $tempRoot 'leaf-unified-local-e2e-20260101-000000'
    [System.IO.Directory]::CreateDirectory($run) | Out-Null
    Assert-Proof ((Remove-ProofRunRoot -Path $run -TempRoot $tempRoot) -and -not [System.IO.Directory]::Exists($run)) 'matching direct child is deleted'
    $other = Join-Path $tempRoot 'unrelated'
    [System.IO.Directory]::CreateDirectory($other) | Out-Null
    Assert-Proof (-not (Remove-ProofRunRoot -Path $other -TempRoot $tempRoot) -and [System.IO.Directory]::Exists($other)) 'nonmatching directory is refused'
    $outside = Join-Path $sandbox 'leaf-console-checkout-20260101-000000'
    [System.IO.Directory]::CreateDirectory($outside) | Out-Null
    Assert-Proof (-not (Remove-ProofRunRoot -Path $outside -TempRoot $tempRoot) -and [System.IO.Directory]::Exists($outside)) 'matching directory outside TempRoot is refused'
    $target = Join-Path $tempRoot 'junction-target'
    [System.IO.Directory]::CreateDirectory($target) | Out-Null
    $sentinel = Join-Path $target 'sentinel.txt'
    [System.IO.File]::WriteAllText($sentinel, 'preserve')
    [System.IO.Directory]::CreateDirectory($run) | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $run 'linked') -Target $target | Out-Null
    Assert-Proof ((Remove-ProofRunRoot -Path $run -TempRoot $tempRoot) -and [System.IO.File]::Exists($sentinel)) 'nested junction deletion preserves the target sentinel'
    $rootLink = Join-Path $tempRoot 'leaf-unified-guest-e2e-20260101-000000'
    New-Item -ItemType Junction -Path $rootLink -Target $target | Out-Null
    Assert-Proof (-not (Remove-ProofRunRoot -Path $rootLink -TempRoot $tempRoot) -and [System.IO.File]::Exists($sentinel)) 'run root that is a junction is refused'
    [System.IO.Directory]::Delete($rootLink)
  }

  Test-ProofCase 'retention' {
    $pruneTemp = Join-Path $sandbox 'prune-temp'
    $pruneCache = Join-Path $sandbox 'prune-cache'
    [System.IO.Directory]::CreateDirectory($pruneTemp) | Out-Null
    $now = [datetime]::Now
    $names = @()
    for ($i = 1; $i -le 8; $i++) {
      $name = 'leaf-unified-local-e2e-20260101-{0:D6}' -f $i
      $names += $name
      $dir = Join-Path $pruneTemp $name
      [System.IO.Directory]::CreateDirectory($dir) | Out-Null
      $age = 48 + ($i / 10.0)
      if ($i -ge 7) { $age = 1 + (($i - 7) / 10.0) }
      [System.IO.Directory]::SetLastWriteTime($dir, $now.AddHours(-$age))
    }
    $old = New-TestRuntime -Root $pruneCache -Key 'aaaaaaaaaaaaaaaa' -Stamp $now.AddDays(-10)
    $fresh = New-TestRuntime -Root $pruneCache -Key 'bbbbbbbbbbbbbbbb' -Stamp $now
    $busy = New-TestRuntime -Root $pruneCache -Key 'cccccccccccccccc' -Stamp $now.AddDays(-10)
    $foreign = Join-Path $pruneCache 'unrelated'
    [System.IO.Directory]::CreateDirectory($foreign) | Out-Null
    $heldLock = [System.IO.File]::Open((Join-Path $pruneCache 'cccccccccccccccc.lock'), [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try {
      $result = Invoke-ProofRetentionPrune -TempRoot $pruneTemp -CacheRoot $pruneCache -Now $now
      $survivors = @(Get-ChildItem -LiteralPath $pruneTemp -Directory | Select-Object -ExpandProperty Name | Sort-Object)
      $expected = @($names[0], $names[6], $names[7]) | Sort-Object
      Assert-Proof (($survivors -join ',') -ceq ($expected -join ',')) 'retention keeps exactly the three newest run roots'
      Assert-Proof ($result.RunRootsRemoved -eq 5) 'retention reports five removed run roots'
      Assert-Proof (-not [System.IO.Directory]::Exists($old) -and $result.RuntimesRemoved -eq 1) 'retention removes the stale runtime'
      Assert-Proof ([System.IO.Directory]::Exists($fresh)) 'retention keeps the fresh runtime'
      Assert-Proof ([System.IO.Directory]::Exists($busy) -and $result.Skipped -ge 1) 'retention skips the locked stale runtime'
      Assert-Proof ([System.IO.Directory]::Exists($foreign)) 'retention leaves unrelated cache content alone'
    } finally { $heldLock.Dispose(); $heldLock = $null }
  }

  Test-ProofCase 'runner source' {
    foreach ($name in @('run_unified_local_proof.ps1', 'run_console_checkout_proof.ps1')) {
      $path = Join-Path $PSScriptRoot "..\$name"
      $tokens = $null
      $parseErrors = $null
      [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors) | Out-Null
      Assert-Proof ($parseErrors.Count -eq 0) "$name parses without errors"
      $source = [System.IO.File]::ReadAllText($path)
      Assert-Proof ($source.Contains('. (Join-Path $PSScriptRoot ''lib\proof-runtime.ps1'')')) "$name dot-sources the runtime helper"
    }
  }
} catch {
  Assert-Proof $false "sandbox setup ($($_.Exception.Message))"
} finally {
  if ($null -ne $heldLock) { $heldLock.Dispose() }
  $env:LEAF_PROOF_TEST_UV_LOG = $savedLog
  $env:LEAF_PROOF_TEST_UV_FAIL = $savedFailure
  try {
    $resolved = [System.IO.Path]::GetFullPath($sandbox)
    if ([System.IO.Path]::GetDirectoryName($resolved).TrimEnd('\', '/') -ine $sandboxParent -or
        [System.IO.Path]::GetFileName($resolved) -cne $sandboxName) { throw 'Unsafe sandbox cleanup path' }
    if ([System.IO.Directory]::Exists($resolved)) { [System.IO.Directory]::Delete($resolved, $true) }
  } catch { Assert-Proof $false "sandbox cleanup ($($_.Exception.Message))" }
}

if ($script:failures -gt 0) { exit 1 }
exit 0
