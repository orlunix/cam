# test-interactive.ps1
# Simulates an interactive CLI tool that uses a prompt and waits for input.
# Tests wintmux's ability to detect prompts and inject responses via send-keys.
#
# Usage:
#   powershell -File test-interactive.ps1

$prompts = @(
    "Enter your name: ",
    "Choose an option (1-3): ",
    "Do you want to continue? (y/n): ",
    "Enter file path: "
)

Write-Host "Interactive Agent Simulator"
Write-Host "=========================="
Write-Host ""

foreach ($prompt in $prompts) {
    Write-Host -NoNewline $prompt
    $input = Read-Host
    Write-Host "  -> Received: '$input'"
    Write-Host ""
    Start-Sleep -Milliseconds 500
}

Write-Host "All prompts answered. Exiting."
