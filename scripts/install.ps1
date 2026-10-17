$ErrorActionPreference = "Stop"

$Package = if ($env:BRAIN_PACKAGE) { $env:BRAIN_PACKAGE } else { "ai-computer-brain" }

function Say($message) { Write-Host "[brain] $message" -ForegroundColor Cyan }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "installing uv, the Python manager (one time)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was installed but is not on PATH yet. Open a new PowerShell window and run this command again."
}

Say "installing AI Computer Brain (Python 3.12 is downloaded if needed)"
uv tool install --python 3.12 --force $Package
try { uv tool update-shell | Out-Null } catch {}
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

Say "installed. Its folder is $env:USERPROFILE\.brain. Starting the console; next time just type: brain"
brain
