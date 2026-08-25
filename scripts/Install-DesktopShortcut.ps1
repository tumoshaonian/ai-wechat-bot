param(
    [string]$ShortcutName = '企业微信电脑助手.lnk'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$launcherPath = Join-Path $projectRoot 'scripts\WeComBotLauncher.ps1'
$sourcePng = Join-Path $projectRoot 'icon\粉色毛茸茸微信图标_爱给网_aigei_com.png'
$iconPath = Join-Path $projectRoot 'icon\wecom-computer-agent.ico'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) $ShortcutName

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Launcher not found: $launcherPath"
}
if (-not (Test-Path -LiteralPath $sourcePng -PathType Leaf)) {
    throw "Icon image not found: $sourcePng"
}

# Modern Windows accepts a PNG-compressed 256x256 image inside an ICO container.
$pngBytes = [System.IO.File]::ReadAllBytes($sourcePng)
$stream = [System.IO.File]::Open($iconPath, [System.IO.FileMode]::Create)
$writer = New-Object System.IO.BinaryWriter($stream)
try {
    $writer.Write([UInt16]0)               # reserved
    $writer.Write([UInt16]1)               # icon type
    $writer.Write([UInt16]1)               # one image
    $writer.Write([Byte]0)                 # width 256
    $writer.Write([Byte]0)                 # height 256
    $writer.Write([Byte]0)                 # palette
    $writer.Write([Byte]0)                 # reserved
    $writer.Write([UInt16]1)               # planes
    $writer.Write([UInt16]32)              # bits per pixel
    $writer.Write([UInt32]$pngBytes.Length)
    $writer.Write([UInt32]22)              # image data offset
    $writer.Write($pngBytes)
} finally {
    $writer.Dispose()
    $stream.Dispose()
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcherPath`""
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Description = '启动并监控企业微信电脑助手'
$shortcut.Save()

Write-Output "SHORTCUT=$shortcutPath"
Write-Output "ICON=$iconPath"
