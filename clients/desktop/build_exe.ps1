# Developer build of MultiChat on Windows. Most people should download MultiChat-Setup.exe from the GitHub releases
# page instead (built by .github/workflows/windows.yml).
#   powershell -ExecutionPolicy Bypass -File clients\desktop\build_exe.ps1
# Output: clients\desktop\dist\MultiChat\MultiChat.exe, plus installer\Output\MultiChat-Setup.exe when Inno Setup 6 is installed.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Run([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe $($arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

# A running MultiChat (it stays in the tray) locks its files; stop it before rebuilding.
$running = Get-Process MultiChat, multichat-approval-bridge -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Stopping the running MultiChat..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 1
}

if (-not (Test-Path .venv)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { Run "py" @("-3", "-m", "venv", ".venv") } else { Run "python" @("-m", "venv", ".venv") }
}
$py = ".\.venv\Scripts\python.exe"
Run $py @("-m", "pip", "install", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "..\core", ".", "pyinstaller>=6", "pillow")
Run $py @("installer\make_icon.py", "installer\multichat.ico")
Run $py @("-m", "PyInstaller", "--noconfirm", "--clean", "multichat.spec")
if (-not (Test-Path dist\MultiChat\MultiChat.exe)) { throw "dist\MultiChat\MultiChat.exe was not created" }

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    Run $iscc @("installer\multichat.iss")
    Write-Host "`nInstaller: clients\desktop\installer\Output\MultiChat-Setup.exe"
}
Write-Host "`nBuilt clients\desktop\dist\MultiChat\MultiChat.exe"
