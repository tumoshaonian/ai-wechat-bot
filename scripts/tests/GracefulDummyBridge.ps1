param(
    [Parameter(Mandatory = $true)][string]$StopFile,
    [Parameter(Mandatory = $true)][string]$AckFile
)

$ErrorActionPreference = 'Stop'
$deadline = [datetime]::UtcNow.AddSeconds(120)
while ([datetime]::UtcNow -lt $deadline) {
    if (Test-Path -LiteralPath $StopFile -PathType Leaf) {
        Get-Content -LiteralPath $StopFile -Raw | Set-Content -LiteralPath $AckFile -Encoding UTF8
        return
    }
    Start-Sleep -Milliseconds 100
}
throw 'Graceful stop request was not received.'
