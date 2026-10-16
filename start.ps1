$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Say($message) { Write-Host "[brain] $message" -ForegroundColor Cyan }
function Fail($message) { Write-Host "[brain] $message" -ForegroundColor Red; exit 1 }

function Find-Python {
    foreach ($candidate in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python3"), @("python"))) {
        $exe = $candidate[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $arguments = @($candidate[1..($candidate.Length - 1)] | Where-Object { $_ }) + @("-c", "import sys; print(sys.version_info >= (3, 11))")
        try { $ok = & $exe @arguments 2>$null } catch { continue }
        if ("$ok".Trim() -eq "True") { return $candidate }
    }
    return $null
}

function Ensure-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) { return $true }
    Say "installing uv, the Python manager (one time)"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    } catch { return $false }
    return [bool](Get-Command uv -ErrorAction SilentlyContinue)
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Say "creating the virtual environment"
        uv venv --python 3.12 .venv | Out-Null
    } else {
        $found = Find-Python
        if ($found) {
            Say "creating the virtual environment with $($found -join ' ')"
            & $found[0] @($found[1..($found.Length - 1)] | Where-Object { $_ }) -m venv .venv
        } elseif (Ensure-Uv) {
            Say "creating the virtual environment"
            uv venv --python 3.12 .venv | Out-Null
        } else {
            Fail "Python 3.11+ was not found and uv could not be installed. Install Python from https://www.python.org/downloads/ and run this script again."
        }
    }
}

& $python -c "import second_brain" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "installing AI Computer Brain and its dependencies (first run only)"
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv pip install --python $python -q -e .
    } else {
        & $python -m pip install -q --upgrade pip
        & $python -m pip install -q -e .
    }
}

Say "starting AI Computer Brain"
& $python -m second_brain.cli shell --open @args
