[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AuditPython,

    [Parameter(Mandatory = $true)]
    [string]$VisualStudioInstall,

    [Parameter(Mandatory = $true)]
    [string]$VisualStudioLayout,

    [Parameter(Mandatory = $true)]
    [string]$VsWhere,

    [Parameter(Mandatory = $true)]
    [string]$WindowsSdkRoot,

    [Parameter(Mandatory = $true)]
    [string]$WindowsSdkVersion,

    [Parameter(Mandatory = $true)]
    [string]$MsvcVersion,

    [Parameter(Mandatory = $true)]
    [string]$PythonRoot,

    [Parameter(Mandatory = $true)]
    [string]$PyInstallerSdist,

    [Parameter(Mandatory = $true)]
    [string]$PyInstallerSource,

    [Parameter(Mandatory = $true)]
    [string]$PackagingDependencies,

    [Parameter(Mandatory = $true)]
    [string]$ProbeWorkRoot,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$SupportVerifiedOn,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [string]$ExpectedLock
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$ResolvedAuditPython = (Resolve-Path -LiteralPath $AuditPython).Path
$ResolvedVisualStudioInstall = (Resolve-Path -LiteralPath $VisualStudioInstall).Path
$ResolvedVisualStudioLayout = (Resolve-Path -LiteralPath $VisualStudioLayout).Path
$ResolvedVsWhere = (Resolve-Path -LiteralPath $VsWhere).Path
$ResolvedWindowsSdkRoot = (Resolve-Path -LiteralPath $WindowsSdkRoot).Path
$ResolvedPythonRoot = (Resolve-Path -LiteralPath $PythonRoot).Path
$ResolvedPyInstallerSdist = (Resolve-Path -LiteralPath $PyInstallerSdist).Path
$ResolvedPyInstallerSource = (Resolve-Path -LiteralPath $PyInstallerSource).Path
$ResolvedPackagingDependencies = (Resolve-Path -LiteralPath $PackagingDependencies).Path
$EntryContract = Join-Path $RepositoryRoot 'packaging\windows\frozen-entry\candidate-contract.json'
if ([string]::IsNullOrWhiteSpace($ExpectedLock)) {
    $ExpectedLock = Join-Path $RepositoryRoot 'packaging\windows\frozen-entry\candidate-input.lock.json'
}
$ResolvedExpectedLock = (Resolve-Path -LiteralPath $ExpectedLock).Path
$ResolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
$OutputParent = Split-Path -Parent $ResolvedOutput
$ResolvedProbeWorkRoot = [System.IO.Path]::GetFullPath($ProbeWorkRoot)
$ProbeParent = Split-Path -Parent $ResolvedProbeWorkRoot

if (-not (Test-Path -LiteralPath $OutputParent -PathType Container)) {
    throw 'OutputPath parent does not exist'
}
if (Test-Path -LiteralPath $ResolvedOutput) {
    throw 'OutputPath must not already exist'
}
if (-not (Test-Path -LiteralPath $ProbeParent -PathType Container)) {
    throw 'ProbeWorkRoot parent does not exist'
}
if (Test-Path -LiteralPath $ResolvedProbeWorkRoot) {
    throw 'ProbeWorkRoot must not already exist'
}

Copy-Item -LiteralPath $ResolvedPyInstallerSource -Destination $ResolvedProbeWorkRoot -Recurse
$ProbeBootloader = Join-Path $ResolvedProbeWorkRoot 'bootloader'
$ProbeWaf = Join-Path $ProbeBootloader 'waf'
$ProbeBuildStdout = Join-Path $ResolvedProbeWorkRoot 'localcat-probe-build.stdout.log'
$ProbeBuildStderr = Join-Path $ResolvedProbeWorkRoot 'localcat-probe-build.stderr.log'
$ProbePrebuildEvidence = Join-Path $ResolvedProbeWorkRoot 'localcat-probe-prebuild-evidence.json'
$ProbeBuildEvidence = Join-Path $ResolvedProbeWorkRoot 'localcat-probe-build-evidence.json'
$VcVarsAll = Join-Path $ResolvedVisualStudioInstall 'VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path -LiteralPath $ProbeWaf -PathType Leaf)) {
    throw 'Copied PyInstaller source does not contain bootloader/waf'
}
if (Test-Path -LiteralPath (Join-Path $ProbeBootloader 'build')) {
    throw 'Copied PyInstaller source contains a pre-existing bootloader build cache'
}
if (-not (Test-Path -LiteralPath $VcVarsAll -PathType Leaf)) {
    throw 'Selected Visual Studio installation does not contain vcvarsall.bat'
}

Push-Location $RepositoryRoot
try {
    & $ResolvedAuditPython -B -m tools.record_windows_frozen_entry_probe_prebuild `
        --source-copy $ResolvedProbeWorkRoot `
        --sdist $ResolvedPyInstallerSdist `
        --output $ProbePrebuildEvidence
    if ($LASTEXITCODE -ne 0) {
        throw "Probe pre-build evidence recording failed with exit $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$BuildEnvironmentNames = @('Path', 'INCLUDE', 'LIB', 'LIBPATH', 'CL', '_CL_', 'LINK', '_LINK_', 'VSCMD_SKIP_SENDTELEMETRY')
$SavedBuildEnvironment = @{}
foreach ($Name in $BuildEnvironmentNames) {
    $SavedBuildEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
}
$WindowsRoot = [Environment]::GetEnvironmentVariable('SystemRoot', 'Process')
if ([string]::IsNullOrWhiteSpace($WindowsRoot)) {
    throw 'SystemRoot is unavailable'
}
$CmdExe = Join-Path $WindowsRoot 'System32\cmd.exe'
if (-not (Test-Path -LiteralPath $CmdExe -PathType Leaf)) {
    throw 'system cmd.exe is unavailable'
}
$ToolchainSetup = 'call "' + $VcVarsAll + '" x64 ' + $WindowsSdkVersion + ' -vcvars_ver=' + $MsvcVersion + ' >nul'
$ToolchainQuery = $ToolchainSetup + ' && set VCToolsVersion && set WindowsSDKVersion && for %I in (cl.exe) do @echo CL=%~$PATH:I && for %I in (link.exe) do @echo LINK=%~$PATH:I && for %I in (rc.exe) do @echo RC=%~$PATH:I && for %I in (mt.exe) do @echo MT=%~$PATH:I'
$BuildCommand = $ToolchainSetup + ' && cd /d "' + $ProbeBootloader + '" && "' + $ResolvedAuditPython + '" -B "' + $ProbeWaf + '" all --target-arch=64bit -j1 1>"' + $ProbeBuildStdout + '" 2>"' + $ProbeBuildStderr + '"'
try {
    [Environment]::SetEnvironmentVariable('Path', (Join-Path $WindowsRoot 'System32') + ';' + $WindowsRoot, 'Process')
    foreach ($Name in @('INCLUDE', 'LIB', 'LIBPATH', 'CL', '_CL_', 'LINK', '_LINK_')) {
        [Environment]::SetEnvironmentVariable($Name, $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('VSCMD_SKIP_SENDTELEMETRY', '1', 'Process')
    $ToolchainLines = @(& $CmdExe /d /s /c $ToolchainQuery)
    if ($LASTEXITCODE -ne 0) {
        throw "Selected toolchain environment failed with exit $LASTEXITCODE"
    }
    $ToolchainFacts = @{}
    foreach ($Line in $ToolchainLines) {
        if ($Line -match '^([^=]+)=(.*)$') {
            $ToolchainFacts[$Matches[1].ToUpperInvariant()] = $Matches[2].Trim()
        }
    }
    foreach ($Key in @('VCTOOLSVERSION', 'WINDOWSSDKVERSION', 'CL', 'LINK', 'RC', 'MT')) {
        if (-not $ToolchainFacts.ContainsKey($Key)) {
            throw "Selected toolchain did not resolve $Key"
        }
    }
    if ($ToolchainFacts['VCTOOLSVERSION'].TrimEnd('\') -ne $MsvcVersion) {
        throw ("vcvarsall selected MSVC '{0}', expected '{1}'" -f $ToolchainFacts['VCTOOLSVERSION'], $MsvcVersion)
    }
    if ($ToolchainFacts['WINDOWSSDKVERSION'].TrimEnd('\') -ne $WindowsSdkVersion) {
        throw ("vcvarsall selected Windows SDK '{0}', expected '{1}'" -f $ToolchainFacts['WINDOWSSDKVERSION'], $WindowsSdkVersion)
    }
    $ExpectedCompilerBin = Join-Path $ResolvedVisualStudioInstall ("VC\Tools\MSVC\{0}\bin\Hostx64\x64" -f $MsvcVersion)
    $ExpectedSdkBin = Join-Path $ResolvedWindowsSdkRoot ("bin\{0}\x64" -f $WindowsSdkVersion)
    $ExpectedTools = @{
        CL = Join-Path $ExpectedCompilerBin 'cl.exe'
        LINK = Join-Path $ExpectedCompilerBin 'link.exe'
        RC = Join-Path $ExpectedSdkBin 'rc.exe'
        MT = Join-Path $ExpectedSdkBin 'mt.exe'
    }
    foreach ($Key in $ExpectedTools.Keys) {
        $Observed = [System.IO.Path]::GetFullPath($ToolchainFacts[$Key])
        $Expected = [System.IO.Path]::GetFullPath($ExpectedTools[$Key])
        if (-not [string]::Equals($Observed, $Expected, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Selected toolchain resolved unexpected $Key path"
        }
    }
    & $CmdExe /d /s /c $BuildCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Selected-toolchain stock probe build failed with exit $LASTEXITCODE"
    }
    Get-Content -LiteralPath $ProbeBuildStdout | Write-Output
    Get-Content -LiteralPath $ProbeBuildStderr | Write-Output
}
finally {
    foreach ($Name in $BuildEnvironmentNames) {
        [Environment]::SetEnvironmentVariable($Name, $SavedBuildEnvironment[$Name], 'Process')
    }
}
$ResolvedToolchainProbeRunw = Join-Path $ProbeBootloader 'build\releasew\runw.exe'
if (-not (Test-Path -LiteralPath $ResolvedToolchainProbeRunw -PathType Leaf)) {
    throw 'Selected-toolchain stock probe did not produce releasew/runw.exe'
}

Push-Location $RepositoryRoot
try {
    & $ResolvedAuditPython -B -m tools.record_windows_frozen_entry_probe `
        --source-copy $ResolvedProbeWorkRoot `
        --sdist $ResolvedPyInstallerSdist `
        --prebuild-evidence $ProbePrebuildEvidence `
        --probe-runw $ResolvedToolchainProbeRunw `
        --cl $ExpectedTools['CL'] `
        --link $ExpectedTools['LINK'] `
        --rc $ExpectedTools['RC'] `
        --mt $ExpectedTools['MT'] `
        --msvc-version $MsvcVersion `
        --sdk-version $WindowsSdkVersion `
        --stdout-log $ProbeBuildStdout `
        --stderr-log $ProbeBuildStderr `
        --output $ProbeBuildEvidence
    if ($LASTEXITCODE -ne 0) {
        throw "Probe build evidence recording failed with exit $LASTEXITCODE"
    }
    & $ResolvedAuditPython -B -m tools.prepare_windows_frozen_entry_inputs `
        --vs-install $ResolvedVisualStudioInstall `
        --vs-layout $ResolvedVisualStudioLayout `
        --vswhere $ResolvedVsWhere `
        --sdk-root $ResolvedWindowsSdkRoot `
        --sdk-version $WindowsSdkVersion `
        --msvc-version $MsvcVersion `
        --python-root $ResolvedPythonRoot `
        --pyinstaller-sdist $ResolvedPyInstallerSdist `
        --pyinstaller-source $ResolvedPyInstallerSource `
        --packaging-dependencies $ResolvedPackagingDependencies `
        --toolchain-probe-runw $ResolvedToolchainProbeRunw `
        --probe-build-evidence $ProbeBuildEvidence `
        --entry-contract $EntryContract `
        --verified-on $SupportVerifiedOn `
        --output $ResolvedOutput
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate input preparation failed with exit $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$ExpectedHash = (Get-FileHash -LiteralPath $ResolvedExpectedLock -Algorithm SHA256).Hash
$ActualHash = (Get-FileHash -LiteralPath $ResolvedOutput -Algorithm SHA256).Hash
if ($ActualHash -ne $ExpectedHash) {
    throw "Candidate input replay mismatch: expected $ExpectedHash, observed $ActualHash"
}

$Record = Get-Content -LiteralPath $ResolvedOutput -Raw | ConvertFrom-Json
[pscustomobject]@{
    Status = 'VERIFIED'
    CandidateInputDigest = $Record.candidate_input_digest
    FileSha256 = $ActualHash
}
