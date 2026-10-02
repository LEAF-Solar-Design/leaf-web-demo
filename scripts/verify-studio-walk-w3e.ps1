$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath 'C:/tmp/wt-swe-w3e'
$env:LEAF_W3E_DRILL_LOG_DIR = 'C:/tmp/swe-w3e-drill-logs'

Push-Location -LiteralPath scripts
try {
    python -P -m pytest test_gate_runner.py test_studio_walk_regression_gate.py -q -p no:cacheprovider
    $rc = $LASTEXITCODE
} finally { Pop-Location }
if ($rc -ne 0) { exit $rc }

Push-Location -LiteralPath scripts
try {
    python -P -m pytest ci/tests/test_selection.py -q -p no:cacheprovider
    $rc = $LASTEXITCODE
} finally { Pop-Location }
if ($rc -ne 0) { exit $rc }

Push-Location -LiteralPath tests
try {
    python -P -m pytest test_codebuild_ci_script.py --deselect test_codebuild_ci_script.py::TestCodebuildCiScript::test_proof_trusted_sha_receipt_contract -q -p no:cacheprovider
    $rc = $LASTEXITCODE
} finally { Pop-Location }
if ($rc -ne 0) { exit $rc }

Push-Location -LiteralPath tests
try {
    python -P -m pytest test_run_all_gates_selection.py -q -p no:cacheprovider
    $rc = $LASTEXITCODE
} finally { Pop-Location }
if ($rc -ne 0) { exit $rc }

Push-Location -LiteralPath tests
try {
    python -P -m pytest test_contract_workflow_shape.py -q -p no:cacheprovider
    $rc = $LASTEXITCODE
} finally { Pop-Location }
if ($rc -ne 0) { exit $rc }

node --test --test-timeout=900000 web/e2e/regressions/expectRed.test.mjs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python scripts/run-all-gates.py --only studio-walk-regressions --jobs 1 --log-dir C:/tmp/swe-w3e-gate-logs
exit $LASTEXITCODE
