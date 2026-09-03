[CmdletBinding()]
param(
    [ValidateSet(
        'run',
        'start-supervisor',
        'status',
        'start',
        'stop',
        'restart',
        'emergency-stop',
        'shutdown'
    )]
    [string]$Action = 'status',

    [ValidateSet('all', 'admin', 'bridge')]
    [string]$Service = 'all',

    [switch]$NoAutoStart,

    [ValidateRange(2, 120)]
    [int]$CommandTimeoutSeconds = 45
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$scriptPath = $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $projectRoot '.env'

function Get-SettingValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowEmptyString()][string]$DefaultValue = ''
    )

    $environmentValue = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($null -ne $environmentValue) {
        return $environmentValue
    }
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        $match = Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue | Where-Object {
            $_ -match ('^\s*' + [regex]::Escape($Name) + '\s*=')
        } | Select-Object -Last 1
        if ($null -ne $match) {
            $value = ($match -split '=', 2)[1].Trim()
            if ($value.Length -ge 2 -and (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )) {
                return $value.Substring(1, $value.Length - 2)
            }
            return $value
        }
    }
    return $DefaultValue
}

function ConvertTo-Boolean {
    param([AllowEmptyString()][string]$Value, [bool]$DefaultValue = $false)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $DefaultValue }
    return $Value.Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on')
}

function Resolve-ConfiguredPath {
    param(
        [AllowEmptyString()][string]$Value,
        [string]$BasePath = $projectRoot
    )
    if ([string]::IsNullOrWhiteSpace($Value)) { return '' }
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $BasePath $Value))
}

$runtimeOverride = Get-SettingValue -Name 'SUPERVISOR_RUNTIME_DIR'
$runtimeDir = if ([string]::IsNullOrWhiteSpace($runtimeOverride)) {
    Join-Path $projectRoot '.runtime\supervisor'
} else {
    Resolve-ConfiguredPath -Value $runtimeOverride
}
$logOverride = Get-SettingValue -Name 'SUPERVISOR_LOG_DIR'
$logDir = if ([string]::IsNullOrWhiteSpace($logOverride)) { $projectRoot } else {
    Resolve-ConfiguredPath -Value $logOverride
}
$commandsDir = Join-Path $runtimeDir 'commands'
$processingDir = Join-Path $runtimeDir 'processing'
$responsesDir = Join-Path $runtimeDir 'responses'
$servicesDir = Join-Path $runtimeDir 'services'
$supervisorStateFile = Join-Path $runtimeDir 'supervisor.json'
$statusFile = Join-Path $runtimeDir 'status.json'
$lockFile = Join-Path $runtimeDir 'supervisor.lock'
$supervisorLog = Join-Path $logDir 'local-supervisor.log'

foreach ($directory in @($runtimeDir, $logDir, $commandsDir, $processingDir, $responsesDir, $servicesDir)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value,
        [int]$Depth = 10
    )
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $temporaryPath = "$Path.$PID.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $Value | ConvertTo-Json -Depth $Depth | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    } finally {
        Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
    }
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
}

function Write-SupervisorLog {
    param([Parameter(Mandatory = $true)][string]$Message, [string]$Level = 'INFO')
    $line = '{0} [{1}] {2}' -f ([datetime]::UtcNow.ToString('o')), $Level.ToUpperInvariant(), $Message
    try { Add-Content -LiteralPath $supervisorLog -Value $line -Encoding UTF8 } catch {}
}

function Get-Sha256 {
    param([AllowEmptyString()][string]$Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-WindowsPowerShellExecutable {
    $systemPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $systemPowerShell -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($systemPowerShell)
    }
    $command = Get-Command 'powershell.exe' -ErrorAction Stop
    return [System.IO.Path]::GetFullPath([string]$command.Source)
}

function ConvertTo-WindowsCommandLineToken {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    $builder.Append('"') | Out-Null
    $backslashCount = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashCount++
            continue
        }
        if ($character -eq '"') {
            if ($backslashCount -gt 0) { $builder.Append((('\' * ($backslashCount * 2)) -join '')) | Out-Null }
            $builder.Append('\"') | Out-Null
            $backslashCount = 0
            continue
        }
        if ($backslashCount -gt 0) { $builder.Append((('\' * $backslashCount) -join '')) | Out-Null }
        $builder.Append($character) | Out-Null
        $backslashCount = 0
    }
    if ($backslashCount -gt 0) { $builder.Append((('\' * ($backslashCount * 2)) -join '')) | Out-Null }
    $builder.Append('"') | Out-Null
    return $builder.ToString()
}

function Join-WindowsCommandLineArguments {
    param([string[]]$Arguments)
    # Start-Process joins its ArgumentList array. Keep every already-quoted
    # token as a separate element; passing one joined scalar makes PowerShell
    # interpret the whole value as its implicit -Command argument.
    return @($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineToken -Value ([string]$_) })
}

function Get-CanonicalServiceCommandLine {
    param([Parameter(Mandatory = $true)]$Definition)
    return ([ordered]@{
        executable = [System.IO.Path]::GetFullPath([string]$Definition.Executable).ToLowerInvariant()
        arguments = @($Definition.Arguments | ForEach-Object { [string]$_ })
        workingDirectory = [System.IO.Path]::GetFullPath([string]$Definition.WorkingDirectory).ToLowerInvariant()
    } | ConvertTo-Json -Depth 5 -Compress)
}

function Get-CanonicalSupervisorCommandLine {
    $powerShellExe = Get-WindowsPowerShellExecutable
    return ([ordered]@{
        executable = [System.IO.Path]::GetFullPath($powerShellExe).ToLowerInvariant()
        arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', [System.IO.Path]::GetFullPath($scriptPath), '-Action', 'run')
        workingDirectory = [System.IO.Path]::GetFullPath($projectRoot).ToLowerInvariant()
    } | ConvertTo-Json -Depth 5 -Compress)
}

function Get-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [AllowEmptyString()][string]$FallbackCommandLine = ''
    )
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $cim = $null
        try { $cim = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop } catch {}
        $executablePath = if ($null -ne $cim) { [string]$cim.ExecutablePath } else { '' }
        if ([string]::IsNullOrWhiteSpace($executablePath)) {
            try { $executablePath = [string]$process.Path } catch { $executablePath = '' }
        }
        if ([string]::IsNullOrWhiteSpace($executablePath)) { return $null }
        $commandLine = if ($null -ne $cim) { [string]$cim.CommandLine } else { '' }
        $commandSource = 'operating_system'
        if ([string]::IsNullOrWhiteSpace($commandLine) -and -not [string]::IsNullOrWhiteSpace($FallbackCommandLine)) {
            $commandLine = $FallbackCommandLine
            $commandSource = 'supervisor_config'
        }
        if ([string]::IsNullOrWhiteSpace($commandLine)) { return $null }
        return [pscustomobject]@{
            pid = $process.Id
            processName = $process.ProcessName
            startedAtUtc = $process.StartTime.ToUniversalTime().ToString('o')
            executablePath = $executablePath
            commandLine = $commandLine
            commandSha256 = Get-Sha256 -Value $commandLine
            commandSource = $commandSource
        }
    } catch {
        return $null
    }
}

function ConvertTo-UtcInstant {
    param([Parameter(Mandatory = $true)]$Value)
    # Windows PowerShell 5.1 ConvertFrom-Json eagerly converts ISO-8601 values
    # to DateTime.  Converting that object to [string] first discards its kind /
    # offset and made an UTC timestamp look eight hours different in UTC+8.
    if ($Value -is [datetime]) {
        return ([datetime]$Value).ToUniversalTime()
    }
    return [datetimeoffset]::Parse(
        [string]$Value,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind
    ).UtcDateTime
}

function Test-RecordedProcess {
    param(
        [Parameter(Mandatory = $true)]$State,
        [AllowEmptyString()][string]$ExpectedExecutable = '',
        [AllowEmptyString()][string]$ExpectedConfiguredCommandSha256 = ''
    )
    $required = @('pid', 'startedAtUtc', 'commandSha256')
    foreach ($property in $required) {
        if ($State.PSObject.Properties.Name -notcontains $property) {
            return [pscustomobject]@{ Valid = $false; Reason = "state_missing_$property"; Identity = $null }
        }
    }
    $fallbackCommandLine = if ($State.PSObject.Properties.Name -contains 'commandLine') { [string]$State.commandLine } else { '' }
    $identity = Get-ProcessIdentity -ProcessId ([int]$State.pid) -FallbackCommandLine $fallbackCommandLine
    if ($null -eq $identity) {
        return [pscustomobject]@{ Valid = $false; Reason = 'process_not_running'; Identity = $null }
    }
    try {
        $recordedStart = ConvertTo-UtcInstant -Value $State.startedAtUtc
        $actualStart = ConvertTo-UtcInstant -Value $identity.startedAtUtc
        if ([math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 2) {
            return [pscustomobject]@{ Valid = $false; Reason = 'pid_start_time_mismatch'; Identity = $identity }
        }
    } catch {
        return [pscustomobject]@{ Valid = $false; Reason = 'invalid_start_time'; Identity = $identity }
    }
    if ([string]$State.commandSha256 -ne [string]$identity.commandSha256) {
        return [pscustomobject]@{ Valid = $false; Reason = 'command_line_mismatch'; Identity = $identity }
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedConfiguredCommandSha256)) {
        if ($State.PSObject.Properties.Name -notcontains 'configuredCommandSha256') {
            return [pscustomobject]@{ Valid = $false; Reason = 'state_missing_configured_command'; Identity = $identity }
        }
        if ([string]$State.configuredCommandSha256 -ne $ExpectedConfiguredCommandSha256) {
            return [pscustomobject]@{ Valid = $false; Reason = 'configured_command_mismatch'; Identity = $identity }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedExecutable)) {
        $actual = [System.IO.Path]::GetFullPath([string]$identity.executablePath)
        $expected = [System.IO.Path]::GetFullPath($ExpectedExecutable)
        if (-not $actual.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            return [pscustomobject]@{ Valid = $false; Reason = 'executable_path_mismatch'; Identity = $identity }
        }
    }
    return [pscustomobject]@{ Valid = $true; Reason = 'ok'; Identity = $identity }
}

function ConvertFrom-ArgumentSetting {
    param([string]$JsonName, [string]$TextName, [string[]]$DefaultArguments = @())
    $json = Get-SettingValue -Name $JsonName
    if (-not [string]::IsNullOrWhiteSpace($json)) {
        try {
            $parsed = $json | ConvertFrom-Json -ErrorAction Stop
            if ($parsed -isnot [System.Array]) { throw "$JsonName must contain an array" }
            $converted = New-Object System.Collections.Generic.List[string]
            foreach ($item in $parsed) { $converted.Add([string]$item) }
            return $converted.ToArray()
        }
        catch { throw "$JsonName must be a JSON string array" }
    }
    $text = Get-SettingValue -Name $TextName
    if ([string]::IsNullOrWhiteSpace($text)) { return @($DefaultArguments) }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($match in [regex]::Matches($text, '(?:"([^"\\]*(?:\\.[^"\\]*)*)"|''([^'']*)''|(\S+))')) {
        if ($match.Groups[1].Success) { $parts.Add($match.Groups[1].Value.Replace('\"', '"')) }
        elseif ($match.Groups[2].Success) { $parts.Add($match.Groups[2].Value) }
        else { $parts.Add($match.Groups[3].Value) }
    }
    return $parts.ToArray()
}

function Get-ServiceDefinitions {
    $bridgeExecutable = Resolve-ConfiguredPath -Value (Get-SettingValue -Name 'BRIDGE_EXECUTABLE' -DefaultValue '.venv\Scripts\wechat-aibot-bridge.exe')
    $adminExecutableSetting = Get-SettingValue -Name 'ADMIN_API_EXECUTABLE'
    $adminDefaultArguments = @()
    if ([string]::IsNullOrWhiteSpace($adminExecutableSetting)) {
        $adminExecutableSetting = '.venv\Scripts\wechat-aibot-admin.exe'
        $entryPointPath = Resolve-ConfiguredPath -Value $adminExecutableSetting
        if (-not (Test-Path -LiteralPath $entryPointPath -PathType Leaf)) {
            $adminExecutableSetting = '.venv\Scripts\python.exe'
            $adminDefaultArguments = @('-m', 'wechat_agent.admin')
        }
    }
    $adminExecutable = Resolve-ConfiguredPath -Value $adminExecutableSetting
    $adminHost = Get-SettingValue -Name 'ADMIN_HOST' -DefaultValue '127.0.0.1'
    if ($adminHost -in @('0.0.0.0', '::', '[::]')) { $adminHost = '127.0.0.1' }
    $adminPort = Get-SettingValue -Name 'ADMIN_PORT' -DefaultValue '8765'
    $adminBaseUrl = (Get-SettingValue -Name 'ADMIN_API_BASE_URL' -DefaultValue "http://${adminHost}:$adminPort").TrimEnd('/')
    $adminHealthUrl = Get-SettingValue -Name 'ADMIN_API_HEALTH_URL' -DefaultValue "$adminBaseUrl/api/admin/v1/health"
    $adminUiUrl = Get-SettingValue -Name 'ADMIN_API_UI_URL' -DefaultValue "$adminBaseUrl/admin/"
    $bridgeShutdownSetting = Get-SettingValue -Name 'BRIDGE_SHUTDOWN_FILE'
    $bridgeShutdownFile = if ([string]::IsNullOrWhiteSpace($bridgeShutdownSetting)) {
        Join-Path $runtimeDir 'bridge.stop.request'
    } else {
        Resolve-ConfiguredPath -Value $bridgeShutdownSetting
    }

    return [ordered]@{
        admin = [pscustomobject]@{
            Name = 'admin'
            DisplayName = 'Admin API'
            Enabled = ConvertTo-Boolean -Value (Get-SettingValue -Name 'ADMIN_API_ENABLED' -DefaultValue 'true') -DefaultValue $true
            Executable = $adminExecutable
            Arguments = @(ConvertFrom-ArgumentSetting -JsonName 'ADMIN_API_ARGUMENTS_JSON' -TextName 'ADMIN_API_ARGUMENTS' -DefaultArguments $adminDefaultArguments)
            WorkingDirectory = Resolve-ConfiguredPath -Value (Get-SettingValue -Name 'ADMIN_API_WORKING_DIRECTORY' -DefaultValue '.')
            OutLog = Join-Path $logDir 'admin-api.out.log'
            ErrLog = Join-Path $logDir 'admin-api.err.log'
            HealthUrl = $adminHealthUrl
            ShutdownUrl = Get-SettingValue -Name 'ADMIN_API_SHUTDOWN_URL'
            ShutdownFile = ''
            UiUrl = $adminUiUrl
        }
        bridge = [pscustomobject]@{
            Name = 'bridge'
            DisplayName = 'WeCom Bridge'
            Enabled = ConvertTo-Boolean -Value (Get-SettingValue -Name 'BRIDGE_ENABLED' -DefaultValue 'true') -DefaultValue $true
            Executable = $bridgeExecutable
            Arguments = @(ConvertFrom-ArgumentSetting -JsonName 'BRIDGE_ARGUMENTS_JSON' -TextName 'BRIDGE_ARGUMENTS')
            WorkingDirectory = Resolve-ConfiguredPath -Value (Get-SettingValue -Name 'BRIDGE_WORKING_DIRECTORY' -DefaultValue '.')
            OutLog = Join-Path $logDir 'wecom-bridge.out.log'
            ErrLog = Join-Path $logDir 'wecom-bridge.err.log'
            HealthUrl = Get-SettingValue -Name 'BRIDGE_HEALTH_URL'
            ShutdownUrl = Get-SettingValue -Name 'BRIDGE_SHUTDOWN_URL'
            ShutdownFile = $bridgeShutdownFile
            UiUrl = ''
        }
    }
}

function Get-ServiceStatePath {
    param([Parameter(Mandatory = $true)][string]$Name)
    return Join-Path $servicesDir "$Name.json"
}

function Get-OwnedService {
    param([Parameter(Mandatory = $true)]$Definition)
    $statePath = Get-ServiceStatePath -Name $Definition.Name
    $state = Read-JsonFile -Path $statePath
    if ($null -eq $state) {
        return [pscustomobject]@{ State = $null; Validation = $null; Process = $null }
    }
    $configuredCommandHash = Get-Sha256 -Value (Get-CanonicalServiceCommandLine -Definition $Definition)
    $validation = Test-RecordedProcess -State $state -ExpectedExecutable $Definition.Executable -ExpectedConfiguredCommandSha256 $configuredCommandHash
    $process = if ($validation.Valid) { Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue } else { $null }
    return [pscustomobject]@{ State = $state; Validation = $validation; Process = $process }
}

function Find-UnmanagedServiceProcesses {
    param([Parameter(Mandatory = $true)]$Definition)
    if (-not (Test-Path -LiteralPath $Definition.Executable -PathType Leaf)) { return @() }
    $expectedPath = [System.IO.Path]::GetFullPath([string]$Definition.Executable)
    $processName = [System.IO.Path]::GetFileNameWithoutExtension($expectedPath)
    # Generic interpreters can legitimately host unrelated workloads. They are
    # never treated as a conflict unless Windows exposes a command line that
    # contains every configured non-option argument.
    $genericInterpreter = $processName.ToLowerInvariant() -in @('python', 'pythonw', 'powershell', 'pwsh')
    $significantArguments = @($Definition.Arguments | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and -not ([string]$_).StartsWith('-') })
    $matches = New-Object System.Collections.Generic.List[object]
    foreach ($process in @(Get-Process -Name $processName -ErrorAction SilentlyContinue)) {
        $path = ''
        try { $path = [string]$process.Path } catch { continue }
        if ([string]::IsNullOrWhiteSpace($path)) { continue }
        if (-not ([System.IO.Path]::GetFullPath($path)).Equals($expectedPath, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($genericInterpreter) {
            $identity = Get-ProcessIdentity -ProcessId $process.Id
            if ($null -eq $identity -or $significantArguments.Count -eq 0) { continue }
            $allPresent = $true
            foreach ($argument in $significantArguments) {
                if ($identity.commandLine.IndexOf([string]$argument, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) {
                    $allPresent = $false
                    break
                }
            }
            if (-not $allPresent) { continue }
        }
        $matches.Add([pscustomobject]@{ pid = $process.Id; processName = $process.ProcessName; executablePath = $path })
    }
    return $matches.ToArray()
}

function Save-OwnedService {
    param(
        [Parameter(Mandatory = $true)]$Definition,
        [Parameter(Mandatory = $true)]$Identity,
        [Parameter(Mandatory = $true)][string]$SupervisorInstanceId
    )
    $configuredCommandLine = Get-CanonicalServiceCommandLine -Definition $Definition
    $state = [ordered]@{
        schemaVersion = 1
        service = $Definition.Name
        instanceId = [guid]::NewGuid().ToString('N')
        supervisorInstanceId = $SupervisorInstanceId
        pid = $Identity.pid
        processName = $Identity.processName
        startedAtUtc = $Identity.startedAtUtc
        executablePath = $Identity.executablePath
        commandLine = $Identity.commandLine
        commandSha256 = $Identity.commandSha256
        commandSource = $Identity.commandSource
        configuredCommandLine = $configuredCommandLine
        configuredCommandSha256 = Get-Sha256 -Value $configuredCommandLine
        configuredExecutable = $Definition.Executable
        configuredArguments = @($Definition.Arguments)
        workingDirectory = $Definition.WorkingDirectory
    }
    Write-AtomicJson -Path (Get-ServiceStatePath -Name $Definition.Name) -Value $state
    return $state
}

function Get-LogTailText {
    param([string[]]$Paths, [int]$Tail = 30)
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            foreach ($line in @(Get-Content -LiteralPath $path -Tail $Tail -ErrorAction SilentlyContinue)) {
                $lines.Add([string]$line)
            }
        }
    }
    return ($lines -join [Environment]::NewLine)
}

function Start-OwnedService {
    param(
        [Parameter(Mandatory = $true)]$Definition,
        [Parameter(Mandatory = $true)][string]$SupervisorInstanceId
    )
    if (-not $Definition.Enabled) {
        return [pscustomobject]@{ service = $Definition.Name; success = $true; status = 'disabled'; message = 'Service is disabled by configuration.' }
    }
    $owned = Get-OwnedService -Definition $Definition
    if ($null -ne $owned.Validation -and $owned.Validation.Valid) {
        return [pscustomobject]@{ service = $Definition.Name; success = $true; status = 'running'; pid = $owned.Process.Id; message = 'Already running.' }
    }
    if ($null -ne $owned.Validation -and $owned.Validation.Reason -ne 'process_not_running') {
        return [pscustomobject]@{
            service = $Definition.Name
            success = $false
            status = 'ownership_validation_failed'
            message = "Refusing to replace unverified state: $($owned.Validation.Reason)"
        }
    }
    Remove-Item -LiteralPath (Get-ServiceStatePath -Name $Definition.Name) -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $Definition.Executable -PathType Leaf)) {
        return [pscustomobject]@{
            service = $Definition.Name
            success = $false
            status = 'not_installed'
            message = "Executable not found: $($Definition.Executable)"
        }
    }
    if (-not (Test-Path -LiteralPath $Definition.WorkingDirectory -PathType Container)) {
        return [pscustomobject]@{
            service = $Definition.Name
            success = $false
            status = 'invalid_working_directory'
            message = "Working directory not found: $($Definition.WorkingDirectory)"
        }
    }
    $unmanaged = @(Find-UnmanagedServiceProcesses -Definition $Definition)
    if ($unmanaged.Count -gt 0) {
        return [pscustomobject]@{
            service = $Definition.Name
            success = $false
            status = 'unmanaged_conflict'
            pids = @($unmanaged | ForEach-Object { $_.pid })
            message = "A matching process is already running without Supervisor ownership (PID(s): $(@($unmanaged.pid) -join ', ')). Stop the legacy instance once before starting it here."
        }
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$Definition.ShutdownFile)) {
        # A previous forced stop may have left an unconsumed request. Never let
        # a newly started Bridge interpret that stale file as a fresh shutdown.
        Remove-Item -LiteralPath $Definition.ShutdownFile -Force -ErrorAction SilentlyContinue
    }
    try {
        $startParameters = @{
            FilePath = $Definition.Executable
            WorkingDirectory = $Definition.WorkingDirectory
            WindowStyle = 'Hidden'
            RedirectStandardOutput = $Definition.OutLog
            RedirectStandardError = $Definition.ErrLog
            PassThru = $true
        }
        if (@($Definition.Arguments).Count -gt 0) {
            $startParameters.ArgumentList = @($Definition.Arguments | ForEach-Object {
                ConvertTo-WindowsCommandLineToken -Value ([string]$_)
            })
        }
        $previousBridgeShutdownFile = $null
        $setBridgeShutdownEnvironment = $Definition.Name -eq 'bridge' -and -not [string]::IsNullOrWhiteSpace([string]$Definition.ShutdownFile)
        if ($setBridgeShutdownEnvironment) {
            $previousBridgeShutdownFile = [Environment]::GetEnvironmentVariable('BRIDGE_SHUTDOWN_FILE', 'Process')
            [Environment]::SetEnvironmentVariable('BRIDGE_SHUTDOWN_FILE', [string]$Definition.ShutdownFile, 'Process')
        }
        try {
            $process = Start-Process @startParameters
        } finally {
            if ($setBridgeShutdownEnvironment) {
                [Environment]::SetEnvironmentVariable('BRIDGE_SHUTDOWN_FILE', $previousBridgeShutdownFile, 'Process')
            }
        }
        Start-Sleep -Milliseconds 300
        if ($process.HasExited) {
            $tail = Get-LogTailText -Paths @($Definition.ErrLog, $Definition.OutLog)
            throw "Process exited during startup (exit code $($process.ExitCode)). $tail"
        }
        $identity = $null
        for ($attempt = 0; $attempt -lt 20 -and $null -eq $identity; $attempt++) {
            $identity = Get-ProcessIdentity -ProcessId $process.Id -FallbackCommandLine (Get-CanonicalServiceCommandLine -Definition $Definition)
            if ($null -eq $identity) { Start-Sleep -Milliseconds 100 }
        }
        if ($null -eq $identity) {
            try { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } catch {}
            throw 'Could not read the child process command line; ownership cannot be established safely.'
        }
        $state = Save-OwnedService -Definition $Definition -Identity $identity -SupervisorInstanceId $SupervisorInstanceId
        Write-SupervisorLog -Message "Started $($Definition.Name) pid=$($state.pid) instance=$($state.instanceId)"
        return [pscustomobject]@{
            service = $Definition.Name
            success = $true
            status = 'started'
            pid = $state.pid
            instanceId = $state.instanceId
            message = 'Started successfully.'
        }
    } catch {
        Write-SupervisorLog -Level 'ERROR' -Message "Failed to start $($Definition.Name): $($_.Exception.Message)"
        return [pscustomobject]@{ service = $Definition.Name; success = $false; status = 'start_failed'; message = $_.Exception.Message }
    }
}

function Wait-ForProcessExit {
    param([int]$ProcessId, [int]$TimeoutSeconds)
    $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([datetime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 200
    }
    return $null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Invoke-ForceProcessTreeStop {
    param([Parameter(Mandatory = $true)]$State, [Parameter(Mandatory = $true)]$Definition)
    $configuredCommandHash = Get-Sha256 -Value (Get-CanonicalServiceCommandLine -Definition $Definition)
    $validation = Test-RecordedProcess -State $State -ExpectedExecutable $Definition.Executable -ExpectedConfiguredCommandSha256 $configuredCommandHash
    if (-not $validation.Valid) {
        throw "Force stop refused because ownership validation failed: $($validation.Reason)"
    }
    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    $result = Start-Process -FilePath $taskkill -ArgumentList @('/PID', [string]$State.pid, '/T', '/F') -WindowStyle Hidden -Wait -PassThru
    if ($result.ExitCode -ne 0 -and -not (Wait-ForProcessExit -ProcessId ([int]$State.pid) -TimeoutSeconds 2)) {
        # Some restricted Windows sessions reject taskkill /T even for a process
        # created by the current user. Revalidate immediately before the narrower
        # root-process fallback; never apply this fallback to an unverified PID.
        $retryValidation = Test-RecordedProcess `
            -State $State `
            -ExpectedExecutable $Definition.Executable `
            -ExpectedConfiguredCommandSha256 $configuredCommandHash
        if (-not $retryValidation.Valid) {
            throw "taskkill failed and fallback ownership validation failed: $($retryValidation.Reason)"
        }
        Stop-Process -Id ([int]$State.pid) -Force -ErrorAction Stop
        if (-not (Wait-ForProcessExit -ProcessId ([int]$State.pid) -TimeoutSeconds 2)) {
            throw "taskkill failed with exit code $($result.ExitCode), and verified root-process fallback did not exit"
        }
    }
}

function Stop-OwnedService {
    param([Parameter(Mandatory = $true)]$Definition, [switch]$Force)
    $statePath = Get-ServiceStatePath -Name $Definition.Name
    $owned = Get-OwnedService -Definition $Definition
    if ($null -eq $owned.State) {
        return [pscustomobject]@{ service = $Definition.Name; success = $true; status = 'stopped'; message = 'No managed instance.' }
    }
    if (-not $owned.Validation.Valid) {
        if ($owned.Validation.Reason -eq 'process_not_running') {
            Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
            return [pscustomobject]@{ service = $Definition.Name; success = $true; status = 'stopped'; message = 'Managed process had already exited.' }
        }
        return [pscustomobject]@{
            service = $Definition.Name
            success = $false
            status = 'ownership_validation_failed'
            message = "Stop refused: $($owned.Validation.Reason)"
        }
    }
    try {
        $forced = [bool]$Force
        if (-not $Force) {
            $gracefulSignalSent = $false
            if (-not [string]::IsNullOrWhiteSpace([string]$Definition.ShutdownFile)) {
                try {
                    Write-AtomicJson -Path $Definition.ShutdownFile -Value ([ordered]@{
                        schemaVersion = 1
                        requestId = [guid]::NewGuid().ToString('N')
                        service = $Definition.Name
                        pid = [int]$owned.State.pid
                        instanceId = [string]$owned.State.instanceId
                        requestedAtUtc = [datetime]::UtcNow.ToString('o')
                        reason = 'supervisor_stop'
                    })
                    $gracefulSignalSent = $true
                    Write-SupervisorLog -Message "Requested graceful $($Definition.Name) shutdown through $($Definition.ShutdownFile)"
                } catch {
                    Write-SupervisorLog -Level 'WARN' -Message "$($Definition.Name) shutdown-file request failed: $($_.Exception.Message)"
                }
            }
            if (-not $gracefulSignalSent -and -not [string]::IsNullOrWhiteSpace($Definition.ShutdownUrl)) {
                try {
                    Invoke-WebRequest -UseBasicParsing -Method Post -Uri $Definition.ShutdownUrl -TimeoutSec 3 | Out-Null
                    $gracefulSignalSent = $true
                } catch {
                    Write-SupervisorLog -Level 'WARN' -Message "$($Definition.Name) shutdown endpoint failed: $($_.Exception.Message)"
                }
            }
            if (-not $gracefulSignalSent) {
                try { $owned.Process.CloseMainWindow() | Out-Null } catch {}
            }
            $graceSeconds = [int](Get-SettingValue -Name 'SUPERVISOR_GRACEFUL_STOP_SECONDS' -DefaultValue '8')
            if (-not (Wait-ForProcessExit -ProcessId $owned.Process.Id -TimeoutSeconds $graceSeconds)) {
                $forced = $true
            }
        }
        if ($forced -and $null -ne (Get-Process -Id $owned.Process.Id -ErrorAction SilentlyContinue)) {
            Invoke-ForceProcessTreeStop -State $owned.State -Definition $Definition
        }
        if (-not (Wait-ForProcessExit -ProcessId $owned.Process.Id -TimeoutSeconds 3)) {
            throw 'Process remained alive after stop request.'
        }
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
        if (-not [string]::IsNullOrWhiteSpace([string]$Definition.ShutdownFile)) {
            Remove-Item -LiteralPath $Definition.ShutdownFile -Force -ErrorAction SilentlyContinue
        }
        Write-SupervisorLog -Message "Stopped $($Definition.Name) pid=$($owned.Process.Id) forced=$forced"
        return [pscustomobject]@{
            service = $Definition.Name
            success = $true
            status = 'stopped'
            forced = $forced
            message = if ($forced) { 'Stopped with verified process-tree fallback.' } else { 'Stopped gracefully.' }
        }
    } catch {
        Write-SupervisorLog -Level 'ERROR' -Message "Failed to stop $($Definition.Name): $($_.Exception.Message)"
        return [pscustomobject]@{ service = $Definition.Name; success = $false; status = 'stop_failed'; message = $_.Exception.Message }
    }
}

function Get-HealthResult {
    param([Parameter(Mandatory = $true)]$Definition, [bool]$Running)
    if (-not $Running) { return [pscustomobject]@{ status = 'stopped'; checkedAtUtc = [datetime]::UtcNow.ToString('o') } }
    if ([string]::IsNullOrWhiteSpace($Definition.HealthUrl)) {
        return [pscustomobject]@{ status = 'running'; checkedAtUtc = [datetime]::UtcNow.ToString('o') }
    }
    try {
        $timeout = [int](Get-SettingValue -Name 'SUPERVISOR_HEALTH_TIMEOUT_SECONDS' -DefaultValue '2')
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Definition.HealthUrl -TimeoutSec $timeout
        return [pscustomobject]@{
            status = if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { 'healthy' } else { 'unhealthy' }
            httpStatus = [int]$response.StatusCode
            checkedAtUtc = [datetime]::UtcNow.ToString('o')
        }
    } catch {
        return [pscustomobject]@{ status = 'unhealthy'; error = $_.Exception.Message; checkedAtUtc = [datetime]::UtcNow.ToString('o') }
    }
}

function Get-ServiceSnapshot {
    param([Parameter(Mandatory = $true)]$Definition)
    $owned = Get-OwnedService -Definition $Definition
    if ($null -eq $owned.State) {
        $unmanaged = @(Find-UnmanagedServiceProcesses -Definition $Definition)
        return [ordered]@{
            name = $Definition.Name
            displayName = $Definition.DisplayName
            enabled = $Definition.Enabled
            status = if ($unmanaged.Count -gt 0) { 'unmanaged_conflict' } elseif ($Definition.Enabled) { 'stopped' } else { 'disabled' }
            managed = $false
            pid = if ($unmanaged.Count -eq 1) { $unmanaged[0].pid } else { $null }
            conflictingPids = @($unmanaged | ForEach-Object { $_.pid })
            health = Get-HealthResult -Definition $Definition -Running $false
            logs = @($Definition.OutLog, $Definition.ErrLog)
        }
    }
    if (-not $owned.Validation.Valid) {
        return [ordered]@{
            name = $Definition.Name
            displayName = $Definition.DisplayName
            enabled = $Definition.Enabled
            status = if ($owned.Validation.Reason -eq 'process_not_running') { 'stopped' } else { 'ownership_validation_failed' }
            managed = $false
            pid = [int]$owned.State.pid
            validationError = $owned.Validation.Reason
            health = Get-HealthResult -Definition $Definition -Running $false
            logs = @($Definition.OutLog, $Definition.ErrLog)
        }
    }
    return [ordered]@{
        name = $Definition.Name
        displayName = $Definition.DisplayName
        enabled = $Definition.Enabled
        status = 'running'
        managed = $true
        pid = [int]$owned.State.pid
        instanceId = [string]$owned.State.instanceId
        startedAtUtc = [string]$owned.State.startedAtUtc
        health = Get-HealthResult -Definition $Definition -Running $true
        logs = @($Definition.OutLog, $Definition.ErrLog)
    }
}

function Write-CurrentStatus {
    param([Parameter(Mandatory = $true)][string]$SupervisorInstanceId, [Parameter(Mandatory = $true)]$Definitions)
    $state = Read-JsonFile -Path $supervisorStateFile
    $services = [ordered]@{}
    foreach ($name in @('admin', 'bridge')) {
        $services[$name] = Get-ServiceSnapshot -Definition $Definitions[$name]
    }
    $status = [ordered]@{
        schemaVersion = 1
        generatedAtUtc = [datetime]::UtcNow.ToString('o')
        supervisor = [ordered]@{
            status = 'running'
            instanceId = $SupervisorInstanceId
            pid = $PID
            startedAtUtc = if ($null -ne $state) { [string]$state.startedAtUtc } else { '' }
        }
        adminUiUrl = [string]$Definitions.admin.UiUrl
        services = $services
    }
    Write-AtomicJson -Path $statusFile -Value $status -Depth 12
    return $status
}

function Get-SupervisorProcess {
    $state = Read-JsonFile -Path $supervisorStateFile
    if ($null -eq $state) { return $null }
    $expectedCommandHash = Get-Sha256 -Value (Get-CanonicalSupervisorCommandLine)
    $validation = Test-RecordedProcess -State $state -ExpectedExecutable (Get-WindowsPowerShellExecutable) -ExpectedConfiguredCommandSha256 $expectedCommandHash
    if (-not $validation.Valid) { return $null }
    if ($state.PSObject.Properties.Name -notcontains 'scriptPath') { return $null }
    $recordedScript = [System.IO.Path]::GetFullPath([string]$state.scriptPath)
    if (-not $recordedScript.Equals([System.IO.Path]::GetFullPath($scriptPath), [System.StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $validation.Identity
}

function Start-SupervisorProcess {
    $existing = Get-SupervisorProcess
    if ($null -ne $existing) { return $existing }
    Remove-Item -LiteralPath $supervisorStateFile -Force -ErrorAction SilentlyContinue
    $powerShellExe = Get-WindowsPowerShellExecutable
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath, '-Action', 'run') | ForEach-Object {
        ConvertTo-WindowsCommandLineToken -Value ([string]$_)
    }
    Start-Process -FilePath $powerShellExe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden | Out-Null
    $deadline = [datetime]::UtcNow.AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 150
        $running = Get-SupervisorProcess
        if ($null -ne $running) { return $running }
    } while ([datetime]::UtcNow -lt $deadline)
    throw "Local Supervisor did not become ready. See $supervisorLog"
}

function Send-SupervisorCommand {
    param([Parameter(Mandatory = $true)][string]$CommandAction, [Parameter(Mandatory = $true)][string]$TargetService)
    $running = Get-SupervisorProcess
    if ($null -eq $running) { throw 'Local Supervisor is not running.' }
    $id = [guid]::NewGuid().ToString('N')
    $requestPath = Join-Path $commandsDir "$([datetime]::UtcNow.ToString('yyyyMMddHHmmssffff'))-$id.json"
    $responsePath = Join-Path $responsesDir "$id.json"
    Write-AtomicJson -Path $requestPath -Value ([ordered]@{
        id = $id
        action = $CommandAction
        service = $TargetService
        requestedAtUtc = [datetime]::UtcNow.ToString('o')
        clientPid = $PID
    })
    $deadline = [datetime]::UtcNow.AddSeconds($CommandTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 100
        $response = Read-JsonFile -Path $responsePath
        if ($null -ne $response) {
            Remove-Item -LiteralPath $responsePath -Force -ErrorAction SilentlyContinue
            return $response
        }
        if ($null -eq (Get-SupervisorProcess)) { throw 'Local Supervisor exited before responding.' }
    } while ([datetime]::UtcNow -lt $deadline)
    throw "Supervisor command timed out after $CommandTimeoutSeconds seconds (request $id)."
}

function Invoke-ServiceCommand {
    param(
        [Parameter(Mandatory = $true)][string]$CommandAction,
        [Parameter(Mandatory = $true)][string]$TargetService,
        [Parameter(Mandatory = $true)][string]$SupervisorInstanceId,
        [Parameter(Mandatory = $true)]$Definitions
    )
    $names = if ($TargetService -eq 'all') { @('admin', 'bridge') } else { @($TargetService) }
    $results = New-Object System.Collections.Generic.List[object]
    if ($CommandAction -in @('stop', 'emergency-stop', 'shutdown')) {
        [array]::Reverse($names)
    }
    foreach ($name in $names) {
        $definition = $Definitions[$name]
        switch ($CommandAction) {
            'start' { $results.Add((Start-OwnedService -Definition $definition -SupervisorInstanceId $SupervisorInstanceId)) }
            'stop' { $results.Add((Stop-OwnedService -Definition $definition)) }
            'emergency-stop' { $results.Add((Stop-OwnedService -Definition $definition -Force)) }
            'restart' {
                $stopResult = Stop-OwnedService -Definition $definition
                $results.Add($stopResult)
                if ($stopResult.success) { $results.Add((Start-OwnedService -Definition $definition -SupervisorInstanceId $SupervisorInstanceId)) }
            }
            'shutdown' { $results.Add((Stop-OwnedService -Definition $definition)) }
            default { throw "Unsupported service command: $CommandAction" }
        }
    }
    $success = @($results | Where-Object { -not $_.success }).Count -eq 0
    return [pscustomobject]@{ success = $success; action = $CommandAction; service = $TargetService; results = $results.ToArray() }
}

function Invoke-PendingCommands {
    param(
        [Parameter(Mandatory = $true)][string]$SupervisorInstanceId,
        [Parameter(Mandatory = $true)]$Definitions
    )
    $shouldExit = $false
    foreach ($requestFile in @(Get-ChildItem -LiteralPath $commandsDir -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object Name)) {
        $claimedPath = Join-Path $processingDir $requestFile.Name
        try {
            Move-Item -LiteralPath $requestFile.FullName -Destination $claimedPath -ErrorAction Stop
        } catch { continue }
        $request = Read-JsonFile -Path $claimedPath
        if ($null -eq $request -or $request.PSObject.Properties.Name -notcontains 'id') {
            Remove-Item -LiteralPath $claimedPath -Force -ErrorAction SilentlyContinue
            continue
        }
        $responsePath = Join-Path $responsesDir "$($request.id).json"
        try {
            $result = Invoke-ServiceCommand -CommandAction ([string]$request.action) -TargetService ([string]$request.service) -SupervisorInstanceId $SupervisorInstanceId -Definitions $Definitions
            $response = [ordered]@{
                id = [string]$request.id
                success = [bool]$result.success
                action = [string]$request.action
                service = [string]$request.service
                completedAtUtc = [datetime]::UtcNow.ToString('o')
                results = @($result.results)
            }
            if ([string]$request.action -eq 'shutdown') { $shouldExit = $true }
        } catch {
            $response = [ordered]@{
                id = [string]$request.id
                success = $false
                action = [string]$request.action
                service = [string]$request.service
                completedAtUtc = [datetime]::UtcNow.ToString('o')
                error = $_.Exception.Message
                results = @()
            }
            Write-SupervisorLog -Level 'ERROR' -Message "Command $($request.action)/$($request.service) failed: $($_.Exception.Message)"
        }
        Write-AtomicJson -Path $responsePath -Value $response -Depth 12
        Remove-Item -LiteralPath $claimedPath -Force -ErrorAction SilentlyContinue
        Write-CurrentStatus -SupervisorInstanceId $SupervisorInstanceId -Definitions $Definitions | Out-Null
    }
    return $shouldExit
}

function Recover-InterruptedCommands {
    foreach ($processingFile in @(Get-ChildItem -LiteralPath $processingDir -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
        $request = Read-JsonFile -Path $processingFile.FullName
        if ($null -eq $request -or $request.PSObject.Properties.Name -notcontains 'id') {
            Remove-Item -LiteralPath $processingFile.FullName -Force -ErrorAction SilentlyContinue
            continue
        }
        $responsePath = Join-Path $responsesDir "$($request.id).json"
        if (Test-Path -LiteralPath $responsePath -PathType Leaf) {
            Remove-Item -LiteralPath $processingFile.FullName -Force -ErrorAction SilentlyContinue
            continue
        }
        # Service commands are designed to be idempotent: start detects a managed
        # instance and stop detects an already-exited one. Requeueing therefore
        # completes a command interrupted by a Supervisor crash without guessing.
        $recoveredPath = Join-Path $commandsDir $processingFile.Name
        Move-Item -LiteralPath $processingFile.FullName -Destination $recoveredPath -Force
        Write-SupervisorLog -Level 'WARN' -Message "Recovered interrupted command $($request.id)"
    }
    $retentionCutoff = [datetime]::UtcNow.AddDays(-1)
    foreach ($responseFile in @(Get-ChildItem -LiteralPath $responsesDir -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
        if ($responseFile.LastWriteTimeUtc -lt $retentionCutoff) {
            Remove-Item -LiteralPath $responseFile.FullName -Force -ErrorAction SilentlyContinue
        }
    }
}

function Run-Supervisor {
    $lockStream = $null
    $instanceId = [guid]::NewGuid().ToString('N')
    try {
        try {
            $lockStream = New-Object System.IO.FileStream(
                $lockFile,
                [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite,
                [System.IO.FileShare]::None
            )
        } catch {
            throw 'Another Local Supervisor instance already owns the runtime lock.'
        }
        $configuredCommandLine = Get-CanonicalSupervisorCommandLine
        $identity = Get-ProcessIdentity -ProcessId $PID -FallbackCommandLine $configuredCommandLine
        if ($null -eq $identity) { throw 'Cannot establish Local Supervisor process identity.' }
        $state = [ordered]@{
            schemaVersion = 1
            instanceId = $instanceId
            pid = $identity.pid
            processName = $identity.processName
            startedAtUtc = $identity.startedAtUtc
            executablePath = $identity.executablePath
            commandLine = $identity.commandLine
            commandSha256 = $identity.commandSha256
            commandSource = $identity.commandSource
            configuredCommandLine = $configuredCommandLine
            configuredCommandSha256 = Get-Sha256 -Value $configuredCommandLine
            scriptPath = $scriptPath
            workingDirectory = $projectRoot
        }
        Write-AtomicJson -Path $supervisorStateFile -Value $state
        $definitions = Get-ServiceDefinitions
        Recover-InterruptedCommands
        Write-SupervisorLog -Message "Local Supervisor started pid=$PID instance=$instanceId interactiveSession=$([System.Diagnostics.Process]::GetCurrentProcess().SessionId)"
        Write-CurrentStatus -SupervisorInstanceId $instanceId -Definitions $definitions | Out-Null
        $lastStatusWrite = [datetime]::MinValue
        $shouldExit = $false
        while (-not $shouldExit) {
            $shouldExit = Invoke-PendingCommands -SupervisorInstanceId $instanceId -Definitions $definitions
            if (([datetime]::UtcNow - $lastStatusWrite).TotalSeconds -ge 2) {
                Write-CurrentStatus -SupervisorInstanceId $instanceId -Definitions $definitions | Out-Null
                $lastStatusWrite = [datetime]::UtcNow
            }
            Start-Sleep -Milliseconds 250
        }
    } catch {
        Write-SupervisorLog -Level 'ERROR' -Message $_.Exception.Message
        throw
    } finally {
        if ($null -ne $lockStream) { $lockStream.Dispose() }
        $current = Read-JsonFile -Path $supervisorStateFile
        if ($null -ne $current -and $current.PSObject.Properties.Name -contains 'instanceId' -and [string]$current.instanceId -eq $instanceId) {
            Remove-Item -LiteralPath $supervisorStateFile -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $statusFile -Force -ErrorAction SilentlyContinue
        }
        Write-SupervisorLog -Message "Local Supervisor stopped pid=$PID instance=$instanceId"
    }
}

function Get-ClientStatus {
    $identity = Get-SupervisorProcess
    $status = Read-JsonFile -Path $statusFile
    if ($null -eq $identity) {
        return [ordered]@{
            schemaVersion = 1
            generatedAtUtc = [datetime]::UtcNow.ToString('o')
            supervisor = [ordered]@{ status = 'stopped'; pid = $null }
            lastKnownStatus = $status
        }
    }
    if ($null -eq $status) {
        return [ordered]@{
            schemaVersion = 1
            generatedAtUtc = [datetime]::UtcNow.ToString('o')
            supervisor = [ordered]@{ status = 'starting'; pid = $identity.pid }
        }
    }
    return $status
}

switch ($Action) {
    'run' {
        Run-Supervisor
    }
    'start-supervisor' {
        $supervisor = Start-SupervisorProcess
        $result = [ordered]@{ success = $true; supervisorPid = $supervisor.pid; status = 'running' }
        if (-not $NoAutoStart) {
            $result.services = Send-SupervisorCommand -CommandAction 'start' -TargetService 'all'
            $result.success = [bool]$result.services.success
        }
        $result | ConvertTo-Json -Depth 12
    }
    'status' {
        Get-ClientStatus | ConvertTo-Json -Depth 12
    }
    'start' {
        Start-SupervisorProcess | Out-Null
        Send-SupervisorCommand -CommandAction 'start' -TargetService $Service | ConvertTo-Json -Depth 12
    }
    'stop' {
        Send-SupervisorCommand -CommandAction 'stop' -TargetService $Service | ConvertTo-Json -Depth 12
    }
    'restart' {
        Send-SupervisorCommand -CommandAction 'restart' -TargetService $Service | ConvertTo-Json -Depth 12
    }
    'emergency-stop' {
        Send-SupervisorCommand -CommandAction 'emergency-stop' -TargetService $Service | ConvertTo-Json -Depth 12
    }
    'shutdown' {
        Send-SupervisorCommand -CommandAction 'shutdown' -TargetService 'all' | ConvertTo-Json -Depth 12
    }
}
