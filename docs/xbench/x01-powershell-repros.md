# X01 synthetic offline replay in Windows PowerShell 5.1

This is Windows replay support for the parent X01 offline checks (steps 1, 3,
4, 6, and 7). Run from the repository root in PowerShell 5.1 with Python 3 and
its standard library. No installed packages or exporter are needed. These are
synthetic arithmetic, tiny ASCII DXF, and local byte-copy exercises. Local PASS
is not X01 staging PASS: it establishes no live performance, customer drawing
correctness, rendering, export fidelity, readiness, or grade.

Paste the following block as one PowerShell script. It creates a fresh,
task-specific folder under the system temporary directory, verifies that it is
outside this checkout, and prints its location. Keep the entire folder outside
git. The three checker self-tests each expect exit **0**. Native Python nonzero
exits do not automatically throw under `$ErrorActionPreference`; each exit is
captured immediately and checked explicitly. File creation, copy, and hash
errors are terminating errors; the catch/finally records the error and all
evidence collected so far before propagating it.

```powershell
$ErrorActionPreference = 'Stop'
$repo = (Get-Location).Path
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('leaf-x01-offline-benchmark-v2-' + [Guid]::NewGuid().ToString('N'))
$repoPrefix = [IO.Path]::GetFullPath($repo).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if ([IO.Path]::GetFullPath($scratch).StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Temporary folder must be outside the checkout.'
}
[IO.Directory]::CreateDirectory($scratch) | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
$evidencePath = Join-Path $scratch 'replay-evidence.json'
$evidence = [ordered]@{
    schema = 'leaf.xbench.x01.powershell-replay.v1'
    evidence_kind = 'synthetic_offline'
    exits = [ordered]@{}
    hashes = [ordered]@{}
    error = $null
}
Write-Output $scratch

try {
    python tools/xbench/x01_offline/check_cases.py --self-test
    $casesExit = $LASTEXITCODE
    $evidence.exits['check_cases'] = $casesExit
    if ($casesExit -ne 0) { throw "check_cases expected exit 0, got $casesExit" }

    python tools/xbench/x01_offline/check_plot_layers.py --self-test
    $layersExit = $LASTEXITCODE
    $evidence.exits['check_plot_layers_self_test'] = $layersExit
    if ($layersExit -ne 0) { throw "check_plot_layers self-test expected exit 0, got $layersExit" }

    python tools/xbench/x01_offline/check_unit_equivalence.py --self-test
    $unitsExit = $LASTEXITCODE
    $evidence.exits['check_unit_equivalence'] = $unitsExit
    if ($unitsExit -ne 0) { throw "check_unit_equivalence expected exit 0, got $unitsExit" }

    # A synthetic MODULES layer with plotting disabled (290 = 0), leaf A1.
    # Alternating code/value lines; ASCII explicitly avoids PS 5.1 UTF-16 output.
    $original = Join-Path $scratch 'synthetic-original.dxf'
    $lines = @(
        '0', 'SECTION', '2', 'TABLES', '0', 'TABLE', '2', 'LAYER', '70', '1',
        '0', 'LAYER', '2', 'MODULES', '70', '0', '62', '7',
        '6', 'CONTINUOUS', '290', '0', '0', 'ENDTAB', '0', 'ENDSEC',
        '0', 'SECTION', '2', 'ENTITIES', '0', 'LINE', '5', 'A1',
        '8', 'MODULES', '10', '0', '20', '0', '30', '0',
        '11', '100', '21', '0', '31', '0', '0', 'ENDSEC', '0', 'EOF'
    )
    [IO.File]::WriteAllText($original, (($lines -join "`r`n") + "`r`n"), [Text.Encoding]::ASCII)

    $layerJson = python tools/xbench/x01_offline/check_plot_layers.py $original --module-layer MODULES
    $layerExit = $LASTEXITCODE
    $evidence.exits['nonplot_layer'] = $layerExit
    $evidence['nonplot_output'] = @($layerJson)
    if ($layerExit -ne 1) { throw "nonplot repro expected exit 1, got $layerExit" }
    $layerResult = ($layerJson -join "`n") | ConvertFrom-Json
    $named = @($layerResult.diagnostics | Where-Object {
        $_.handle -eq 'A1' -and $_.effective_layer -eq 'MODULES' -and $_.reason -eq 'layer_nonplot'
    })
    if ($layerResult.verdict -ne 'FAIL' -or $named.Count -eq 0) {
        throw 'Expected FAIL with A1 on MODULES and layer_nonplot; parser refusal is insufficient.'
    }

    # Do not create this input: only its parent scratch directory exists.
    $missing = Join-Path $scratch 'intentionally-absent.dxf'
    if (Test-Path -LiteralPath $missing) { throw 'Missing-file repro input unexpectedly exists.' }
    $missingJson = python tools/xbench/x01_offline/check_plot_layers.py $missing --module-layer MODULES
    $missingExit = $LASTEXITCODE
    $evidence.exits['missing_file'] = $missingExit
    $evidence['missing_output'] = @($missingJson)
    if ($missingExit -ne 4) { throw "missing-file repro expected exit 4, got $missingExit" }
    $missingResult = ($missingJson -join "`n") | ConvertFrom-Json
    if ($missingResult.verdict -ne 'UNREADABLE') { throw 'Missing file must be UNREADABLE.' }

    # Hash original bytes around a local copy. Copy/hash failures must stop replay.
    $copy = Join-Path $scratch 'synthetic-copy.dxf'
    $before = (Get-FileHash -LiteralPath $original -Algorithm SHA256 -ErrorAction Stop).Hash
    $evidence.hashes['original_before'] = $before
    Copy-Item -LiteralPath $original -Destination $copy -ErrorAction Stop
    $after = (Get-FileHash -LiteralPath $original -Algorithm SHA256 -ErrorAction Stop).Hash
    $copied = (Get-FileHash -LiteralPath $copy -Algorithm SHA256 -ErrorAction Stop).Hash
    $evidence.hashes['original_after'] = $after
    $evidence.hashes['copy'] = $copied
    $evidence['original_unchanged'] = ($before -eq $after)
    $evidence['copy_equal'] = ($before -eq $copied)
    if ($before -ne $after -or $before -ne $copied) { throw 'Synthetic copy hash equality failed.' }

    # Separate deliberate-mutation exercise, on another synthetic copy only.
    $mutation = Join-Path $scratch 'synthetic-deliberate-mutation.dxf'
    Copy-Item -LiteralPath $original -Destination $mutation -ErrorAction Stop
    $mutationBefore = (Get-FileHash -LiteralPath $mutation -Algorithm SHA256 -ErrorAction Stop).Hash
    $evidence.hashes['mutation_before'] = $mutationBefore
    [IO.File]::AppendAllText($mutation, "`r`n", [Text.Encoding]::ASCII)
    $mutationAfter = (Get-FileHash -LiteralPath $mutation -Algorithm SHA256 -ErrorAction Stop).Hash
    $evidence.hashes['mutation_after'] = $mutationAfter
    $evidence['mutation_detected'] = ($mutationBefore -ne $mutationAfter)
    if ($mutationBefore -ne $before -or $mutationBefore -eq $mutationAfter) {
        throw 'Expected the separate synthetic mutation to change its SHA256.'
    }

    # Bounded baseline: expected exit 0, JSON stdout retained outside git.
    $benchmarkJson = python tools/xbench/x01_offline/benchmark_synthetic.py --sizes '4,8,16' --repeats 1
    $benchmarkExit = $LASTEXITCODE
    $evidence.exits['benchmark'] = $benchmarkExit
    $evidence['benchmark_output'] = @($benchmarkJson)
    if ($benchmarkExit -ne 0) { throw "benchmark expected exit 0, got $benchmarkExit" }
    $benchmarkText = $benchmarkJson -join "`n"
    $baseline = $benchmarkText | ConvertFrom-Json
    if ($baseline.schema -ne 'leaf.xbench.x01.offline-benchmark.v1' -or $baseline.evidence_kind -ne 'synthetic_offline') {
        throw 'Unexpected benchmark JSON schema or evidence kind.'
    }
    [IO.File]::WriteAllText((Join-Path $scratch 'benchmark-baseline.json'), ($benchmarkText + "`n"), $utf8)
}
catch {
    $evidence.error = $_.Exception.Message
    throw
}
finally {
    $evidenceText = $evidence | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($evidencePath, ($evidenceText + "`n"), $utf8)
    Write-Output $evidencePath
}
```

The nonplot repro expects exit **1**, diagnostic `A1` / `MODULES` /
`layer_nonplot`. The missing-file repro expects exit **4**, `UNREADABLE`.
The unchanged original and its local copy must have equal SHA256 hashes; the
separate deliberate-mutation file must have unequal before/after hashes. A
successful copy alone makes no claim about an export or its validation.
`replay-evidence.json` retains native exit codes, layer diagnostics, hash
comparisons, and any error. A missing hash or copy error is an incomplete replay,
never evidence of equality. If writing evidence itself fails, PowerShell reports
that error; do not treat an absent report as success.

The benchmark defaults to sizes `4,8,16`, one repeat. Sizes must be strictly
increasing positive integers at most 64; repeats are limited to 1 through 3.
At most 258 coordinate-grid cells and 2016 tag pairs are allowed per workload.
Geometry, fill coverage, and tag overlap are measured separately with
`perf_counter` and `tracemalloc`. Full analytic expected outputs are checked
before timing and again after each measurement. Workload construction and these
comparisons are excluded from the reported interval. The report includes each
size, repeat, family, elapsed seconds, peak traced bytes, and known output counts
or areas. Peak traced bytes describe Python allocations, not process RSS.

Retain `benchmark-baseline.json` and `replay-evidence.json` together outside git.
Record the checkout revision, Python version, and local runtime conditions with
that scratch evidence when comparing future runs. The root verifier will measure
the actual baseline; this document supplies no invented measurements. There are
no performance pass thresholds. Timing and allocation values vary by runtime
and machine even though the workload and analytic outcomes are deterministic.
Normal benchmark runs use no network or subprocesses. To check its bounded
stdlib self-tests separately (expected exit **0**):

```powershell
python tools/xbench/x01_offline/benchmark_synthetic.py --self-test
$benchmarkSelfTestExit = $LASTEXITCODE
if ($benchmarkSelfTestExit -ne 0) { throw "benchmark self-test expected exit 0, got $benchmarkSelfTestExit" }
```
