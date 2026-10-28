<#
Safe local verification of the evaluation pipeline. Read-mostly:

  * runs tests, dry-runs, and ONE retrieval-only evaluation (no LLM calls, no cost)
  * writes only NEW files under evals\results (a new run folder, a diagnostics JSON,
    and a transcript in evals\results\_verification). Earlier runs are never touched.
  * never reads or prints .env, never runs --mode full, never changes git state
  * makes no LangSmith call unless you pass -LangSmithProbe

Usage (repository root):
  .\scripts\verify_local.ps1
  .\scripts\verify_local.ps1 -SkipRetrieval          # tests and dry-runs only
  .\scripts\verify_local.ps1 -LangSmithProbe         # also submit ONE non-LLM test run to LangSmith
#>
param(
    [switch]$SkipRetrieval,
    [switch]$LangSmithProbe,
    [string]$Org = 'nimbus-crm-demo'
)

$ErrorActionPreference = 'Continue'
$py = '.\.venv313\Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Output "Missing $py - run this from the repository root."; exit 2 }

$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$logDir = 'evals\results\_verification'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$transcript = Join-Path $logDir "verify_$stamp.txt"
Start-Transcript -Path $transcript | Out-Null

$script:summary = @()
function Step([string]$name, [scriptblock]$cmd) {
    Write-Output ""
    Write-Output "=== $name"
    & $cmd
    $code = $LASTEXITCODE
    Write-Output "--- exit code: $code"
    $script:summary += [pscustomobject]@{ Step = $name; ExitCode = $code }
}

Step 'git: branch' { git branch --show-current }
Step 'git: status (short)' { git status --short }
Step 'langsmith SDK installed? (import check; the venv has no pip)' {
    & $py -c "import importlib.util,sys; ok = importlib.util.find_spec('langsmith') is not None; print('langsmith importable:', ok); sys.exit(0 if ok else 1)"
}
Step 'langsmith probe: plan only (no network)' { & $py -m evals.langsmith_probe }
Step 'tests: evaluation files' {
    & $py -m pytest -q tests\test_eval_candidates.py tests\test_eval_rule_analysis.py tests\test_eval_diagnose.py `
        tests\test_eval_metrics.py tests\test_eval_schema.py tests\test_eval_evaluators.py tests\test_eval_runner.py `
        tests\test_eval_cli.py tests\test_eval_routes.py tests\test_tracing.py tests\test_langsmith_sync.py tests\test_langsmith_probe.py
}
Step 'tests: full suite' { & $py -m pytest -q }
Step 'dry-run: candidate dataset (60 cases, unreviewed)' {
    & $py -m evals.run --dataset evals\datasets\nimbuscrm_candidates_v2.jsonl --dry-run
}
Step 'dry-run: golden dataset' { & $py -m evals.run --dry-run }

if (-not $SkipRetrieval) {
    $runDir = $null
    Step 'retrieval-only evaluation (golden dataset, no LLM)' {
        & $py -m evals.run --mode retrieval --org $Org 2>&1 | Tee-Object -Variable captured | ForEach-Object { "$_" }
        $line = ($captured | ForEach-Object { "$_" } | Select-String -Pattern '^\s*results:\s*(.+)$' | Select-Object -Last 1)
        if ($line) { $script:runDir = $line.Matches[0].Groups[1].Value.Trim() }
        # Tee-Object resets the exit code; re-derive it from whether a run was written.
        if (-not $script:runDir) { $global:LASTEXITCODE = 1 } else { $global:LASTEXITCODE = 0 }
    }
    if ($script:runDir -and (Test-Path $script:runDir)) {
        $runId = Split-Path $script:runDir -Leaf
        Write-Output "New run: $runId  ($script:runDir)"
        Step 'diagnose empty retrievals of that run -> JSON (read-only)' {
            & $py -m evals.diagnose_retrieval --run $script:runDir --json "$logDir\diagnose_$runId.json" --top 4
        }
    } else {
        Write-Output "No run directory was reported; skipping diagnostics."
    }
}

if ($LangSmithProbe) {
    Step 'langsmith probe: submit ONE non-LLM run and read it back' { & $py -m evals.langsmith_probe --confirm }
}

Write-Output ""
Write-Output "=== SUMMARY (exit code 0 = success)"
foreach ($row in $script:summary) { Write-Output ("{0,-66} exit {1}" -f $row.Step, $row.ExitCode) }
Write-Output "Transcript: $transcript"
Stop-Transcript | Out-Null
