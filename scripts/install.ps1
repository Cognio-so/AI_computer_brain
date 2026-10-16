$ErrorActionPreference = "Stop"

$Repo = if ($env:BRAIN_REPO_URL) { $env:BRAIN_REPO_URL } else { "https://github.com/ai-computer-brain/ai-computer-brain" }
$Dir = if ($env:BRAIN_INSTALL_DIR) { $env:BRAIN_INSTALL_DIR } else { Join-Path $HOME "ai-computer-brain" }

function Say($message) { Write-Host "[brain] $message" -ForegroundColor Cyan }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Say "installing git (one time)"
    winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
    $env:Path = "$env:ProgramFiles\Git\cmd;$env:Path"
}

if (-not (Test-Path (Join-Path $Dir ".git"))) {
    Say "downloading AI Computer Brain into $Dir"
    git clone --depth 1 $Repo $Dir
}

Set-Location $Dir
Say "setting up and starting the console (the first run asks a few questions)"
& (Join-Path $Dir "start.ps1")
