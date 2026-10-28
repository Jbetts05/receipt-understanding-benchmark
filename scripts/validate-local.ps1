[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Virtual environment not found. Create .venv before validation."
}

& $python -c "import sys; assert sys.prefix != sys.base_prefix, 'not running in a virtual environment'"
if ($LASTEXITCODE -ne 0) {
    throw "Virtual environment verification failed"
}

& $python -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "Dependency consistency check failed"
}

& $python -m ruff check .
if ($LASTEXITCODE -ne 0) {
    throw "Ruff failed"
}

& $python -m mypy
if ($LASTEXITCODE -ne 0) {
    throw "Mypy failed"
}

& $python -m pytest
if ($LASTEXITCODE -ne 0) {
    throw "Pytest failed"
}

Push-Location (Join-Path $PSScriptRoot "..\frontend")
try {
    npm test -- --run
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend tests failed"
    }
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed"
    }
}
finally {
    Pop-Location
}

az bicep build --file (Join-Path $PSScriptRoot "..\infra\main.bicep") --stdout | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Bicep build failed"
}

& $python (Join-Path $PSScriptRoot "..\infra\scripts\stage-package.py") --dry-run
if ($LASTEXITCODE -ne 0) {
    throw "Deployment package validation failed"
}
