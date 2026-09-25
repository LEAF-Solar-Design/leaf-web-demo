function Get-ProofRuntimeKey {
  param(
    [Parameter(Mandatory = $true)][string[]]$RequirementFiles,
    [Parameter(Mandatory = $true)][string]$PythonVersion
  )

  $stream = [System.IO.MemoryStream]::new()
  $writer = [System.IO.BinaryWriter]::new($stream)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    # Length-prefixed fields keep file boundaries unambiguous, including binary bytes.
    $writer.Write($PythonVersion)
    foreach ($file in $RequirementFiles) {
      if (-not [System.IO.File]::Exists($file)) { throw "Missing proof runtime requirement file: $file" }
      $writer.Write([System.IO.Path]::GetFileName($file))
      $bytes = [System.IO.File]::ReadAllBytes($file)
      $writer.Write($bytes.Length)
      $writer.Write($bytes)
    }
    $writer.Flush()
    $hash = $sha.ComputeHash($stream.ToArray())
    return ([System.BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()).Substring(0, 16)
  } finally {
    $sha.Dispose()
    $writer.Dispose()
    $stream.Dispose()
  }
}

function Resolve-ProofRuntimeCacheRoot {
  param([string]$CacheRoot)
  if (-not $CacheRoot) {
    if ($env:LEAF_PROOF_RUNTIME_CACHE) { $CacheRoot = $env:LEAF_PROOF_RUNTIME_CACHE }
    else { $CacheRoot = Join-Path $env:LOCALAPPDATA 'LeafAutomation\proof-runtimes' }
  }
  return [System.IO.Path]::GetFullPath($CacheRoot)
}

function Remove-ProofDirectoryTree {
  param([string]$Path)
  if (([System.IO.File]::GetAttributes($Path) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Refusing to remove reparse point proof root: $Path"
  }
  # CLR 4 recursive deletion can fail on junctions. Unlink them without visiting their targets.
  $pending = [System.Collections.Generic.Stack[string]]::new()
  $pending.Push($Path)
  while ($pending.Count -gt 0) {
    $directory = $pending.Pop()
    if (([System.IO.File]::GetAttributes($directory) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw "Refusing to traverse changed proof directory: $directory"
    }
    foreach ($child in [System.IO.Directory]::GetDirectories($directory)) {
      if (([System.IO.File]::GetAttributes($child) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        [System.IO.Directory]::Delete($child, $false)
      } else {
        $pending.Push($child)
      }
    }
  }
  [System.IO.Directory]::Delete($Path, $true)
}

function Remove-ProofCachedRuntime {
  param([string]$Path, [string]$CacheRoot)
  $p = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
  $root = [System.IO.Path]::GetFullPath($CacheRoot).TrimEnd('\', '/')
  $parent = [System.IO.Path]::GetDirectoryName($p).TrimEnd('\', '/')
  if (-not [string]::Equals($parent, $root, [System.StringComparison]::OrdinalIgnoreCase) -or
      [System.IO.Path]::GetFileName($p) -cnotmatch '^[a-f0-9]{16}$') {
    throw "Refusing to remove proof runtime: $p"
  }
  if (-not [System.IO.Directory]::Exists($p)) { return }
  if (([System.IO.File]::GetAttributes($p) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Refusing to remove reparse point proof runtime: $p"
  }
  Remove-ProofDirectoryTree -Path $p
}

function Get-ProofRuntime {
  param(
    [Parameter(Mandatory = $true)][string[]]$RequirementFiles,
    [string]$PythonVersion = '3.13',
    [string]$CacheRoot,
    [string]$UvCommand = 'uv',
    [ValidateRange(0, 2147483647)][int]$LockTimeoutSeconds = 600
  )

  $key = Get-ProofRuntimeKey -RequirementFiles $RequirementFiles -PythonVersion $PythonVersion
  $CacheRoot = Resolve-ProofRuntimeCacheRoot -CacheRoot $CacheRoot
  [System.IO.Directory]::CreateDirectory($CacheRoot) | Out-Null
  $runtime = Join-Path $CacheRoot $key
  $python = Join-Path $runtime 'Scripts\python.exe'
  $marker = Join-Path $runtime '.leaf-proof-complete'
  $lockPath = Join-Path $CacheRoot "$key.lock"
  $lock = $null
  $timer = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    while ($null -eq $lock) {
      try {
        $lock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
      } catch [System.IO.IOException] {
        if ($timer.Elapsed.TotalSeconds -ge $LockTimeoutSeconds) {
          throw "Timed out waiting for proof runtime lock $lockPath"
        }
        Start-Sleep -Milliseconds 500
      }
    }
    if ([System.IO.Directory]::Exists($runtime)) {
      if (([System.IO.File]::GetAttributes($runtime) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing reparse point proof runtime: $runtime"
      }
      if ([System.IO.File]::Exists($marker) -and
          ([System.IO.File]::GetAttributes($marker) -band [System.IO.FileAttributes]::ReparsePoint) -eq 0 -and
          [System.IO.File]::ReadAllText($marker) -ceq $key -and [System.IO.File]::Exists($python)) {
        [System.IO.File]::SetLastWriteTime($marker, [datetime]::Now)
        return $python
      }
      Remove-ProofCachedRuntime -Path $runtime -CacheRoot $CacheRoot
    }

    $failureMessage = 'Could not create the managed proof Python runtime'
    try {
      & $UvCommand venv $runtime --python $PythonVersion | Out-Null
      if ($LASTEXITCODE -ne 0) { throw $failureMessage }
      $failureMessage = 'Could not install the managed proof Python dependencies'
      $requirementArgs = @('pip', 'install', '--python', $python)
      foreach ($file in $RequirementFiles) { $requirementArgs += @('-r', $file) }
      & $UvCommand @requirementArgs | Out-Null
      if ($LASTEXITCODE -ne 0) { throw $failureMessage }
      if (-not [System.IO.File]::Exists($python)) { throw $failureMessage }
      [System.IO.File]::WriteAllText($marker, $key)
    } catch {
      Remove-ProofCachedRuntime -Path $runtime -CacheRoot $CacheRoot
      throw $failureMessage
    }
    return $python
  } finally {
    if ($null -ne $lock) { $lock.Dispose() }
    $timer.Stop()
  }
}

function Remove-ProofRunRoot {
  param([string]$Path, [string]$TempRoot = [System.IO.Path]::GetTempPath())
  try {
    if (-not $Path -or -not $TempRoot) { return $false }
    $p = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $root = [System.IO.Path]::GetFullPath($TempRoot).TrimEnd('\', '/')
    $parent = [System.IO.Path]::GetDirectoryName($p).TrimEnd('\', '/')
    if (-not [string]::Equals($parent, $root, [System.StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetFileName($p) -notmatch '^leaf-(unified-(local|guest)-e2e|console-checkout)-\d{8}-\d{6}$' -or
        -not [System.IO.Directory]::Exists($p)) { return $false }
    if (([System.IO.File]::GetAttributes($p) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    Remove-ProofDirectoryTree -Path $p
    return $true
  } catch { return $false }
}

function Invoke-ProofRetentionPrune {
  param(
    [string]$TempRoot = [System.IO.Path]::GetTempPath(),
    [string]$CacheRoot,
    [double]$RunRootMaxAgeHours = 24,
    [int]$KeepRunRoots = 3,
    [double]$RuntimeMaxAgeDays = 7,
    [datetime]$Now = [datetime]::Now
  )

  $result = [pscustomobject]@{ RunRootsRemoved = 0; RuntimesRemoved = 0; Skipped = 0 }
  try {
    if ($KeepRunRoots -lt 0 -or $RunRootMaxAgeHours -lt 0 -or $RuntimeMaxAgeDays -lt 0) {
      throw 'Invalid proof retention limits'
    }
    $runCutoff = $Now.AddHours(-$RunRootMaxAgeHours)
    $runtimeCutoff = $Now.AddDays(-$RuntimeMaxAgeDays)
    try {
      $runs = @(Get-ChildItem -LiteralPath $TempRoot -Directory -ErrorAction Stop |
        Where-Object { $_.Name -match '^leaf-(unified-(local|guest)-e2e|console-checkout)-\d{8}-\d{6}$' } |
        Sort-Object LastWriteTime -Descending)
      for ($i = $KeepRunRoots; $i -lt $runs.Count; $i++) {
        if ($result.RunRootsRemoved -ge 200) { break }
        if ($runs[$i].LastWriteTime -ge $runCutoff) { continue }
        if (Remove-ProofRunRoot -Path $runs[$i].FullName -TempRoot $TempRoot) { $result.RunRootsRemoved++ }
        else { $result.Skipped++ }
      }
    } catch { $result.Skipped++ }

    $CacheRoot = Resolve-ProofRuntimeCacheRoot -CacheRoot $CacheRoot
    if ([System.IO.Directory]::Exists($CacheRoot)) {
      foreach ($runtime in (Get-ChildItem -LiteralPath $CacheRoot -Directory -ErrorAction Stop)) {
        if (($result.RunRootsRemoved + $result.RuntimesRemoved) -ge 200) { break }
        if ($runtime.Name -cnotmatch '^[a-f0-9]{16}$') { continue }
        $lock = $null
        try {
          if (($runtime.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $result.Skipped++; continue }
          $marker = Join-Path $runtime.FullName '.leaf-proof-complete'
          if (-not [System.IO.File]::Exists($marker)) { continue }
          if (([System.IO.File]::GetAttributes($marker) -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $result.Skipped++; continue }
          if ([System.IO.File]::ReadAllText($marker) -cne $runtime.Name) { continue }
          if ([System.IO.File]::GetLastWriteTime($marker) -ge $runtimeCutoff) { continue }
          $lockPath = Join-Path $CacheRoot "$($runtime.Name).lock"
          $lock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
          # A builder may have refreshed the marker between enumeration and the lock.
          if (-not [System.IO.File]::Exists($marker) -or
              [System.IO.File]::ReadAllText($marker) -cne $runtime.Name -or
              [System.IO.File]::GetLastWriteTime($marker) -ge $runtimeCutoff) { $result.Skipped++; continue }
          Remove-ProofCachedRuntime -Path $runtime.FullName -CacheRoot $CacheRoot
          $result.RuntimesRemoved++
        } catch { $result.Skipped++ } finally {
          if ($null -ne $lock) { $lock.Dispose() }
        }
      }
    }
  } catch { $result.Skipped++ }
  return $result
}
