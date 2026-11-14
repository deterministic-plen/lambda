# Install the Lambda + Pi VS Code extension without a build step.
# Usage:  powershell -ExecutionPolicy Bypass -File editors\install_vscode.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $here "vscode"
$dst = Join-Path $env:USERPROFILE ".vscode\extensions\lambda-language-0.1.0"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item (Join-Path $src "*") $dst -Recurse -Force
Write-Host "Installed Lambda + Pi highlighting to:"
Write-Host "  $dst"
Write-Host "Fully restart VS Code (quit all windows), then open a .lambda or .pi file."
