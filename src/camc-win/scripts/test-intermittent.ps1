# test-intermittent.ps1
# Simulates a process that runs in bursts with pauses between them.
# Models an AI agent that stops for user confirmation (CAM auto-confirms).
#
# Usage:
#   powershell -File test-intermittent.ps1 [-Cycles 5]

param(
    [int]$Cycles = 5
)

Write-Host "Intermittent agent started"
Write-Host "Cycles: $Cycles"
Write-Host ""

for ($i = 1; $i -le $Cycles; $i++) {
    Write-Host "=== Cycle $i/$Cycles ==="
    Write-Host "Processing..."

    # Burst of output
    for ($j = 1; $j -le 5; $j++) {
        Write-Host "  Step $($j): analyzing module_$($j * $i)..."
        Start-Sleep -Milliseconds 500
    }

    Write-Host ""

    if ($i -lt $Cycles) {
        Write-Host "Do you want to continue? (y/n)"
        # In real CAM usage, CAM detects this prompt via capture-pane
        # and auto-sends 'y' + Enter via send-keys.
        # Here we simulate a wait period.
        Start-Sleep -Seconds 2
        Write-Host "y"
        Write-Host "Continuing..."
    }

    Write-Host ""
}

Write-Host "All cycles complete."
