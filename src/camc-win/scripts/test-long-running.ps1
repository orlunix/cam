# test-long-running.ps1
# Simulates a long-running AI coding agent that outputs periodic status updates.
# Designed to test wintmux capture-pane with a continuously running process.
#
# Usage:
#   powershell -File test-long-running.ps1 [-Duration 60] [-IntervalSeconds 3]

param(
    [int]$Duration = 60,
    [int]$IntervalSeconds = 3
)

$startTime = Get-Date
$endTime = $startTime.AddSeconds($Duration)

$states = @(
    "Planning...",
    "Analyzing codebase...",
    "Thinking...",
    "Editing src/main.py...",
    "Writing tests...",
    "Running tests...",
    "Refactoring...",
    "Reviewing changes..."
)

$outputs = @(
    "  Reading file: config.yaml",
    "  Applying changes to 3 files...",
    "  Test results: 5 passed, 0 failed",
    "  Committing changes...",
    "  Checking dependencies...",
    "  Formatting code...",
    "  Updating imports...",
    "  Validating schema..."
)

$counter = 0
Write-Host "Agent started at $($startTime.ToString('HH:mm:ss'))"
Write-Host "Working directory: $(Get-Location)"
Write-Host "Duration: $Duration seconds"
Write-Host ""

while ((Get-Date) -lt $endTime) {
    $state = $states[$counter % $states.Count]
    $detail = $outputs[$counter % $outputs.Count]

    Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] $state"
    Write-Host $detail

    $counter++
    Start-Sleep -Seconds $IntervalSeconds
}

Write-Host ""
Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] Done! Task completed successfully."
Write-Host "Total iterations: $counter"
