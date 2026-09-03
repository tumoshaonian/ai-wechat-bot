Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$scriptsDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$supervisorScript = Join-Path $scriptsDir 'LocalSupervisor.ps1'
$launcherScript = Join-Path $scriptsDir 'WeComBotLauncher.ps1'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("wecom-supervisor-test-" + [guid]::NewGuid().ToString('N'))
$runtimeDir = Join-Path $testRoot 'runtime'
$logDir = Join-Path $testRoot 'logs'
$savedEnvironment = @{}
$environmentNames = @(
    'SUPERVISOR_RUNTIME_DIR',
    'SUPERVISOR_LOG_DIR',
    'SUPERVISOR_GRACEFUL_STOP_SECONDS',
    'ADMIN_API_ENABLED',
    'BRIDGE_ENABLED',
    'BRIDGE_EXECUTABLE',
    'BRIDGE_ARGUMENTS_JSON',
    'BRIDGE_WORKING_DIRECTORY',
    'BRIDGE_HEALTH_URL',
    'BRIDGE_SHUTDOWN_FILE'
)

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Invoke-SupervisorTestAction {
    param([string]$Action, [string]$Service = 'all', [switch]$NoAutoStart)
    $output = if ($NoAutoStart) {
        & $supervisorScript -Action $Action -Service $Service -CommandTimeoutSeconds 10 -NoAutoStart
    } else {
        & $supervisorScript -Action $Action -Service $Service -CommandTimeoutSeconds 10
    }
    $text = (@($output) -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return $text | ConvertFrom-Json -ErrorAction Stop
}

foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    New-Item -ItemType Directory -Path $runtimeDir, $logDir -Force | Out-Null
    # Codex and some terminals run under pwsh with a bundled PSHOME that does
    # not contain powershell.exe.  The managed dummy must use the same stable
    # Windows PowerShell binary that LocalSupervisor itself validates.
    $powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShellExe -PathType Leaf)) {
        $powerShellExe = (Get-Command 'powershell.exe' -ErrorAction Stop).Source
    }
    $shutdownFile = Join-Path $runtimeDir 'bridge.stop.request'
    $shutdownAckFile = Join-Path $runtimeDir 'bridge.stop.ack.json'
    $dummyWorker = Join-Path $PSScriptRoot 'GracefulDummyBridge.ps1'
    [Environment]::SetEnvironmentVariable('SUPERVISOR_RUNTIME_DIR', $runtimeDir, 'Process')
    [Environment]::SetEnvironmentVariable('SUPERVISOR_LOG_DIR', $logDir, 'Process')
    [Environment]::SetEnvironmentVariable('SUPERVISOR_GRACEFUL_STOP_SECONDS', '1', 'Process')
    [Environment]::SetEnvironmentVariable('ADMIN_API_ENABLED', 'false', 'Process')
    [Environment]::SetEnvironmentVariable('BRIDGE_ENABLED', 'true', 'Process')
    [Environment]::SetEnvironmentVariable('BRIDGE_EXECUTABLE', $powerShellExe, 'Process')
    [Environment]::SetEnvironmentVariable('BRIDGE_ARGUMENTS_JSON', (@('-NoProfile', '-File', $dummyWorker, '-StopFile', $shutdownFile, '-AckFile', $shutdownAckFile) | ConvertTo-Json -Compress), 'Process')
    [Environment]::SetEnvironmentVariable('BRIDGE_WORKING_DIRECTORY', $testRoot, 'Process')
    [Environment]::SetEnvironmentVariable('BRIDGE_HEALTH_URL', '', 'Process')
    [Environment]::SetEnvironmentVariable('BRIDGE_SHUTDOWN_FILE', $shutdownFile, 'Process')

    $launcherSource = Get-Content -LiteralPath $launcherScript -Raw
    Assert-True -Condition ($launcherSource -notmatch 'Get-NetTCPConnection') -Message 'Launcher must never adopt a process by port.'
    Assert-True -Condition ($launcherSource -notmatch 'LocalPort\s+8080') -Message 'Launcher must not target the legacy Java port.'
    Assert-True -Condition ($launcherSource -match '打开管理后台') -Message 'Launcher must expose the Admin UI action.'
    Assert-True -Condition ($launcherSource -match '紧急强停') -Message 'Launcher must retain the emergency stop action.'

    $startedSupervisor = Invoke-SupervisorTestAction -Action 'start-supervisor' -NoAutoStart
    Assert-True -Condition ([bool]$startedSupervisor.success) -Message 'Supervisor should start.'
    Assert-True -Condition ([int]$startedSupervisor.supervisorPid -gt 0) -Message 'Supervisor PID should be recorded.'

    $startedBridge = Invoke-SupervisorTestAction -Action 'start' -Service 'bridge'
    Assert-True -Condition ([bool]$startedBridge.success) -Message 'Managed dummy service should start.'
    $bridgeStartResult = @($startedBridge.results)[0]
    Assert-True -Condition ([int]$bridgeStartResult.pid -gt 0) -Message 'Managed service PID should be returned.'

    Start-Sleep -Milliseconds 500
    $status = Invoke-SupervisorTestAction -Action 'status'
    Assert-True -Condition ($status.supervisor.status -eq 'running') -Message 'Status should report a running supervisor.'
    Assert-True -Condition ($status.services.bridge.status -eq 'running') -Message 'Status should report a running managed service.'
    Assert-True -Condition ([bool]$status.services.bridge.managed) -Message 'Running service should be marked managed.'

    $serviceStatePath = Join-Path $runtimeDir 'services\bridge.json'
    $originalState = Get-Content -LiteralPath $serviceStatePath -Raw
    $tamperedState = $originalState | ConvertFrom-Json
    $tamperedState.commandSha256 = ('0' * 64)
    $tamperedState | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $serviceStatePath -Encoding UTF8
    $refusedStop = Invoke-SupervisorTestAction -Action 'stop' -Service 'bridge'
    $tamperDiagnostics = @(
        Get-Content -LiteralPath (Join-Path $logDir 'wecom-bridge.err.log') -Raw -ErrorAction SilentlyContinue
        Get-Content -LiteralPath (Join-Path $logDir 'wecom-bridge.out.log') -Raw -ErrorAction SilentlyContinue
    ) -join ' | '
    Assert-True -Condition (-not [bool]$refusedStop.success) -Message ("Stop must be refused after command fingerprint tampering. response=" + ($refusedStop | ConvertTo-Json -Depth 10 -Compress) + " logs=$tamperDiagnostics")
    Assert-True -Condition (@($refusedStop.results)[0].status -eq 'ownership_validation_failed') -Message 'Refusal should expose the ownership error.'
    $earlyWorkerDiagnostics = @(
        Get-Content -LiteralPath (Join-Path $logDir 'wecom-bridge.err.log') -Raw -ErrorAction SilentlyContinue
        Get-Content -LiteralPath (Join-Path $logDir 'wecom-bridge.out.log') -Raw -ErrorAction SilentlyContinue
    ) -join ' | '
    Assert-True -Condition ($null -ne (Get-Process -Id ([int]$bridgeStartResult.pid) -ErrorAction SilentlyContinue)) -Message "Refused stop must not kill the process. logs=$earlyWorkerDiagnostics"

    $originalState | Set-Content -LiteralPath $serviceStatePath -Encoding UTF8
    $stoppedBridge = Invoke-SupervisorTestAction -Action 'stop' -Service 'bridge'
    Assert-True -Condition ([bool]$stoppedBridge.success) -Message ("Verified managed service should stop. Response: " + ($stoppedBridge | ConvertTo-Json -Depth 10 -Compress))
    Assert-True -Condition ($null -eq (Get-Process -Id ([int]$bridgeStartResult.pid) -ErrorAction SilentlyContinue)) -Message 'Managed process tree should be gone.'
    $workerDiagnostics = @(
        Get-Content -LiteralPath (Join-Path $logDir 'wecom-bridge.err.log') -Raw -ErrorAction SilentlyContinue
        Get-Content -LiteralPath (Join-Path $logDir 'wecom-bridge.out.log') -Raw -ErrorAction SilentlyContinue
    ) -join ' | '
    Assert-True -Condition (Test-Path -LiteralPath $shutdownAckFile -PathType Leaf) -Message ("Dummy Bridge must observe the graceful shutdown request file. stop=" + ($stoppedBridge | ConvertTo-Json -Depth 10 -Compress) + " logs=$workerDiagnostics")
    $shutdownRequest = Get-Content -LiteralPath $shutdownAckFile -Raw | ConvertFrom-Json
    Assert-True -Condition ($shutdownRequest.service -eq 'bridge') -Message 'Shutdown request must target Bridge.'
    Assert-True -Condition ([int]$shutdownRequest.pid -eq [int]$bridgeStartResult.pid) -Message 'Shutdown request must carry the owned PID.'
    Assert-True -Condition (-not [string]::IsNullOrWhiteSpace([string]$shutdownRequest.instanceId)) -Message 'Shutdown request must carry the managed instance ID.'
    Assert-True -Condition (-not [bool]@($stoppedBridge.results)[0].forced) -Message 'File-aware Bridge should stop gracefully without taskkill fallback.'

    $shutdown = Invoke-SupervisorTestAction -Action 'shutdown'
    Assert-True -Condition ([bool]$shutdown.success) -Message 'Supervisor shutdown should succeed.'
    Start-Sleep -Milliseconds 700
    $finalStatus = Invoke-SupervisorTestAction -Action 'status'
    Assert-True -Condition ($finalStatus.supervisor.status -eq 'stopped') -Message 'Supervisor should exit after shutdown.'

    Write-Output 'PASS: Local Supervisor lifecycle, ownership validation, safe stop, status and launcher safety checks.'
} finally {
    try {
        $stateFile = Join-Path $runtimeDir 'services\bridge.json'
        if (Test-Path -LiteralPath $stateFile) {
            $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
            $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
            if ($null -ne $process -and [string]$state.commandLine -match [regex]::Escape($testRoot)) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        & $supervisorScript -Action shutdown -CommandTimeoutSeconds 3 2>$null | Out-Null
    } catch {}
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
