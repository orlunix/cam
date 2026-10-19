# test-cam-workflow.ps1
# Full integration test simulating the CAM workflow with wintmux.
#
# Tests all the tmux commands CAM uses:
#   1. new-session  — Create a session running a long process
#   2. set-option   — Increase scrollback buffer
#   3. has-session  — Verify session is alive
#   4. capture-pane — Read process output
#   5. send-keys    — Inject input (literal + special keys)
#   6. pipe-pane    — Stream output to log file
#   7. kill-session — Terminate session
#   8. has-session  — Confirm session is gone
#
# Usage:
#   cd <wintmux project root>
#   .\scripts\test-cam-workflow.ps1

$ErrorActionPreference = "Stop"

$wintmux = ".\wintmux.exe"
$socketDir = "$env:TEMP\wintmux-test"
$socket = "$socketDir\test-session.sock"
$session = "test-session"
$logFile = "$socketDir\test-output.log"
$passed = 0
$failed = 0

function Assert-ExitCode {
    param([int]$Expected, [string]$TestName)
    if ($LASTEXITCODE -eq $Expected) {
        Write-Host "  PASS" -ForegroundColor Green
        $script:passed++
    } else {
        Write-Host "  FAIL (exit=$LASTEXITCODE, expected=$Expected)" -ForegroundColor Red
        $script:failed++
    }
}

# Clean up previous runs
Remove-Item $socketDir -Recurse -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $socketDir -Force | Out-Null

Write-Host "=====================================" -ForegroundColor Cyan
Write-Host " WinTmux CAM Workflow Integration Test" -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# ---- Test 0: Version ----
Write-Host "[Test 0] Version check" -ForegroundColor Yellow
& $wintmux -V
Assert-ExitCode 0 "Version"
Write-Host ""

# ---- Test 1: Create session ----
Write-Host "[Test 1] new-session (long-running process)" -ForegroundColor Yellow
& $wintmux -S $socket new-session -d -s $session -c $env:TEMP "powershell -File $PSScriptRoot\test-long-running.ps1 -Duration 30 -IntervalSeconds 2"
Assert-ExitCode 0 "new-session"
Write-Host ""

Start-Sleep -Seconds 3

# ---- Test 2: Set option ----
Write-Host "[Test 2] set-option history-limit 50000" -ForegroundColor Yellow
& $wintmux -S $socket set-option -t $session history-limit 50000
Assert-ExitCode 0 "set-option"
Write-Host ""

# ---- Test 3: Has-session (should exist) ----
Write-Host "[Test 3] has-session (should exist)" -ForegroundColor Yellow
& $wintmux -S $socket has-session -t $session
Assert-ExitCode 0 "has-session exists"
Write-Host ""

# ---- Test 4: Capture pane ----
Write-Host "[Test 4] capture-pane" -ForegroundColor Yellow
$output = & $wintmux -S $socket capture-pane -p -J -t "${session}:0.0" -S -20
Assert-ExitCode 0 "capture-pane"
Write-Host "  Captured output:" -ForegroundColor DarkGray
$output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
Write-Host ""

# ---- Test 5: Send keys (literal text) ----
Write-Host "[Test 5] send-keys -l (literal text)" -ForegroundColor Yellow
& $wintmux -S $socket send-keys -t "${session}:0.0" -l -- "echo hello from wintmux"
Assert-ExitCode 0 "send-keys literal"
Write-Host ""

# ---- Test 6: Send keys (Enter) ----
Write-Host "[Test 6] send-keys Enter" -ForegroundColor Yellow
& $wintmux -S $socket send-keys -t "${session}:0.0" Enter
Assert-ExitCode 0 "send-keys Enter"
Start-Sleep -Seconds 1
Write-Host ""

# ---- Test 7: Capture again to see injected input ----
Write-Host "[Test 7] capture-pane (after send-keys)" -ForegroundColor Yellow
$output = & $wintmux -S $socket capture-pane -p -J -t "${session}:0.0" -S -30
Assert-ExitCode 0 "capture-pane after input"
Write-Host "  Captured output:" -ForegroundColor DarkGray
$output | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
Write-Host ""

# ---- Test 8: Pipe-pane ----
Write-Host "[Test 8] pipe-pane (stream to log file)" -ForegroundColor Yellow
& $wintmux -S $socket pipe-pane -t "${session}:0.0" "cat >> $logFile"
Assert-ExitCode 0 "pipe-pane"
Start-Sleep -Seconds 3
if (Test-Path $logFile) {
    $size = (Get-Item $logFile).Length
    Write-Host "  Log file created: $logFile ($size bytes)" -ForegroundColor DarkGray
} else {
    Write-Host "  Warning: log file not created yet" -ForegroundColor DarkYellow
}
Write-Host ""

# ---- Test 9: Kill session ----
Write-Host "[Test 9] kill-session" -ForegroundColor Yellow
& $wintmux -S $socket kill-session -t $session
Assert-ExitCode 0 "kill-session"
Start-Sleep -Seconds 2
Write-Host ""

# ---- Test 10: Has-session (should NOT exist) ----
Write-Host "[Test 10] has-session (should not exist)" -ForegroundColor Yellow
& $wintmux -S $socket has-session -t $session
Assert-ExitCode 1 "has-session gone"
Write-Host ""

# ---- Summary ----
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host " Results: $passed passed, $failed failed" -ForegroundColor $(if ($failed -eq 0) { "Green" } else { "Red" })
Write-Host "=====================================" -ForegroundColor Cyan

# Clean up
Remove-Item $socketDir -Recurse -ErrorAction SilentlyContinue

if ($failed -gt 0) { exit 1 }
