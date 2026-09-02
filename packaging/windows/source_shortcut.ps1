param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Create", "Inspect")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$ShortcutPath,

    [string]$TargetPath,
    [string]$Arguments,
    [string]$WorkingDirectory,
    [string]$IconLocation,
    [string]$Description
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$shell = New-Object -ComObject WScript.Shell

if ($Mode -eq "Create") {
    foreach ($required in @($TargetPath, $Arguments, $WorkingDirectory, $IconLocation, $Description)) {
        if ([string]::IsNullOrWhiteSpace($required)) {
            throw "Shortcut metadata is incomplete"
        }
    }
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.IconLocation = $IconLocation
    $shortcut.Description = $Description
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        throw "Shortcut was not created"
    }
    exit 0
}

$shortcut = $shell.CreateShortcut($ShortcutPath)
[ordered]@{
    target_path = $shortcut.TargetPath
    arguments = $shortcut.Arguments
    working_directory = $shortcut.WorkingDirectory
    icon_location = $shortcut.IconLocation
    description = $shortcut.Description
} | ConvertTo-Json -Compress
