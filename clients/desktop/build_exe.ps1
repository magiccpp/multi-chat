# Build MultiChat.exe on Windows.
#   powershell -ExecutionPolicy Bypass -File clients\desktop\build_exe.ps1
# Output: clients\desktop\dist\MultiChat\MultiChat.exe (zip that folder to distribute).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py -3.12" } else { $python = "python" }
if (-not (Test-Path .venv)) { Invoke-Expression "$python -m venv .venv" }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install ..\core . "pyinstaller>=6"
& .\.venv\Scripts\pyinstaller.exe --noconfirm --clean multichat.spec

Write-Host ""
Write-Host "Built dist\MultiChat\MultiChat.exe"
Write-Host "Run it, enter the server URL and token, then open Runner settings to let models work on this PC."
