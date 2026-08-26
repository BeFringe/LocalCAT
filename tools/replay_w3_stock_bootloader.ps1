[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$BaselineArtifactRoot,

    [Parameter(Mandatory = $true)]
    [string]$WorkRoot,

    [Parameter(Mandatory = $true)]
    [string]$OutputRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ResolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$ResolvedBaseline = (Resolve-Path -LiteralPath $BaselineArtifactRoot).Path
$ResolvedWorkParent = Split-Path -Parent ([System.IO.Path]::GetFullPath($WorkRoot))
$ResolvedOutputParent = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutputRoot))

if (-not (Test-Path -LiteralPath $ResolvedWorkParent -PathType Container)) {
    throw 'WorkRoot parent does not exist'
}
if (-not (Test-Path -LiteralPath $ResolvedOutputParent -PathType Container)) {
    throw 'OutputRoot parent does not exist'
}
if (Test-Path -LiteralPath $WorkRoot) {
    throw 'WorkRoot must not already exist'
}
if (Test-Path -LiteralPath $OutputRoot) {
    throw 'OutputRoot must not already exist'
}

$AuditVenv = Join-Path $WorkRoot 'audit-venv'
$DownloadRoot = Join-Path $WorkRoot 'downloads'
$SourceRootParent = Join-Path $WorkRoot 'sources'
New-Item -ItemType Directory -Path $DownloadRoot | Out-Null
New-Item -ItemType Directory -Path $SourceRootParent | Out-Null
New-Item -ItemType Directory -Path $OutputRoot | Out-Null

& $ResolvedPython -m venv $AuditVenv
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to create the clean audit venv'
}
$AuditPython = Join-Path $AuditVenv 'Scripts\python.exe'
& $AuditPython -m pip install --disable-pip-version-check --only-binary=:all: --no-deps 'PyInstaller==6.22.2' 'pefile==2024.8.26'
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to install the pinned audit packages'
}
& $AuditPython -m pip download --disable-pip-version-check --no-deps --no-binary=:all: --dest $DownloadRoot 'PyInstaller==6.22.2'
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to download the pinned PyInstaller sdist'
}

$SourceArchive = Join-Path $DownloadRoot 'pyinstaller-6.22.2.tar.gz'
if (-not (Test-Path -LiteralPath $SourceArchive -PathType Leaf)) {
    throw 'Pinned PyInstaller sdist was not materialized'
}
& $AuditPython -m tarfile -e $SourceArchive $SourceRootParent
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to extract the pinned PyInstaller sdist'
}

$PythonBase = (& $ResolvedPython -c 'import sys; print(sys.base_prefix)').Trim()
$PythonDll = Join-Path $PythonBase 'python314.dll'
$StockBootloader = Join-Path $AuditVenv 'Lib\site-packages\PyInstaller\bootloader\Windows-64bit-intel\runw.exe'
$ExtractedSource = Join-Path $SourceRootParent 'pyinstaller-6.22.2'
$BaselineExe = Join-Path $ResolvedBaseline 'dist-clean\LocalCAT-ui-mvp\LocalCAT-ui-mvp.exe'
$BaselineMatrix = Join-Path $ResolvedBaseline 'matrix.json'
$BaselineInventory = Join-Path $ResolvedBaseline 'dist-clean-sha256.json'
$BaselineChecksums = Join-Path $ResolvedBaseline 'checksums.sha256'
$BaselineAnalysis = Join-Path $ResolvedBaseline 'build-clean\LocalCAT-ui-mvp\Analysis-00.toc'
$AuditJson = Join-Path $OutputRoot 'stock-w3-audit.json'
$AuditLog = Join-Path $OutputRoot 'run.log'

& $AuditPython -B (Join-Path $RepositoryRoot 'tools\audit_w3_stock_bootloader.py') `
    --stock-bootloader $StockBootloader `
    --baseline-exe $BaselineExe `
    --python-dll $PythonDll `
    --source-archive $SourceArchive `
    --source-root $ExtractedSource `
    --baseline-matrix $BaselineMatrix `
    --baseline-inventory $BaselineInventory `
    --baseline-checksums $BaselineChecksums `
    --baseline-analysis $BaselineAnalysis `
    --output $AuditJson `
    --log-output $AuditLog

if ($LASTEXITCODE -ne 1) {
    throw "Expected stock W3 NO_GO exit 1, received $LASTEXITCODE"
}
Get-Content -LiteralPath $AuditLog
