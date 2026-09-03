param(
    [switch]$NoAutoStart
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $projectRoot '.env'
$supervisorScript = Join-Path $PSScriptRoot 'LocalSupervisor.ps1'

if (-not (Test-Path -LiteralPath $supervisorScript -PathType Leaf)) {
    throw "Local Supervisor not found: $supervisorScript"
}

function Get-DotEnvValue {
    param([string]$Name, [AllowEmptyString()][string]$DefaultValue = '')
    $environmentValue = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($null -ne $environmentValue) { return $environmentValue }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { return $DefaultValue }
    $match = Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue | Where-Object {
        $_ -match ('^\s*' + [regex]::Escape($Name) + '\s*=')
    } | Select-Object -Last 1
    if ($null -eq $match) { return $DefaultValue }
    $value = ($match -split '=', 2)[1].Trim()
    if ($value.Length -ge 2 -and (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    )) {
        return $value.Substring(1, $value.Length - 2)
    }
    return $value
}

$runtimeSetting = Get-DotEnvValue -Name 'SUPERVISOR_RUNTIME_DIR'
$runtimeDir = if ([string]::IsNullOrWhiteSpace($runtimeSetting)) {
    Join-Path $projectRoot '.runtime\supervisor'
} elseif ([System.IO.Path]::IsPathRooted($runtimeSetting)) {
    [System.IO.Path]::GetFullPath($runtimeSetting)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $runtimeSetting))
}
$logSetting = Get-DotEnvValue -Name 'SUPERVISOR_LOG_DIR'
$logDir = if ([string]::IsNullOrWhiteSpace($logSetting)) {
    $projectRoot
} elseif ([System.IO.Path]::IsPathRooted($logSetting)) {
    [System.IO.Path]::GetFullPath($logSetting)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $projectRoot $logSetting))
}
$statusFile = Join-Path $runtimeDir 'status.json'
$adminOutLog = Join-Path $logDir 'admin-api.out.log'
$adminErrLog = Join-Path $logDir 'admin-api.err.log'
$supervisorLog = Join-Path $logDir 'local-supervisor.log'
$bridgeOutLog = Join-Path $logDir 'wecom-bridge.out.log'
$bridgeErrLog = Join-Path $logDir 'wecom-bridge.err.log'
$desktopWorkerLog = Join-Path $projectRoot 'desktop-worker.log'
$javaOutLog = Join-Path $projectRoot 'java-backend.out.log'
$javaErrLog = Join-Path $projectRoot 'java-backend.err.log'
$adminHost = Get-DotEnvValue -Name 'ADMIN_HOST' -DefaultValue '127.0.0.1'
if ($adminHost -in @('0.0.0.0', '::', '[::]')) { $adminHost = '127.0.0.1' }
$adminPort = Get-DotEnvValue -Name 'ADMIN_PORT' -DefaultValue '8765'
$adminBaseUrl = (Get-DotEnvValue -Name 'ADMIN_API_BASE_URL' -DefaultValue "http://${adminHost}:$adminPort").TrimEnd('/')
$script:adminUiUrl = Get-DotEnvValue -Name 'ADMIN_API_UI_URL' -DefaultValue "$adminBaseUrl/admin/"

function Invoke-SupervisorAction {
    param(
        [ValidateSet('start-supervisor', 'start', 'stop', 'restart', 'emergency-stop', 'shutdown')]
        [string]$Action,
        [ValidateSet('all', 'admin', 'bridge')]
        [string]$Service = 'all',
        [switch]$DoNotAutoStart
    )
    $raw = if ($DoNotAutoStart) {
        & $supervisorScript -Action $Action -Service $Service -NoAutoStart
    } else {
        & $supervisorScript -Action $Action -Service $Service
    }
    $text = (@($raw) -join [Environment]::NewLine).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    $result = $text | ConvertFrom-Json -ErrorAction Stop
    if ($result.PSObject.Properties.Name -contains 'success' -and -not [bool]$result.success) {
        $messages = @()
        if ($result.PSObject.Properties.Name -contains 'results') {
            $messages = @($result.results | Where-Object { -not $_.success } | ForEach-Object { "$($_.service): $($_.message)" })
        }
        if ($result.PSObject.Properties.Name -contains 'services' -and $null -ne $result.services) {
            $messages += @($result.services.results | Where-Object { -not $_.success } | ForEach-Object { "$($_.service): $($_.message)" })
        }
        if ($messages.Count -eq 0 -and $result.PSObject.Properties.Name -contains 'error') { $messages = @([string]$result.error) }
        throw (($messages | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join [Environment]::NewLine)
    }
    return $result
}

function Read-SupervisorStatus {
    if (-not (Test-Path -LiteralPath $statusFile -PathType Leaf)) { return $null }
    try {
        $status = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json -ErrorAction Stop
        $generated = [datetime]::Parse([string]$status.generatedAtUtc).ToUniversalTime()
        if (([datetime]::UtcNow - $generated).TotalSeconds -gt 8) { return $null }
        return $status
    } catch {
        return $null
    }
}

function Read-LogTail {
    param([string[]]$Paths, [int]$Tail = 250)
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $content = @(Get-Content -LiteralPath $path -Tail $Tail -ErrorAction SilentlyContinue)
            if ($content.Count -gt 0) {
                $parts.Add("===== $([System.IO.Path]::GetFileName($path)) =====")
                foreach ($line in $content) { $parts.Add([string]$line) }
            }
        }
    }
    return ($parts.ToArray() -join [Environment]::NewLine)
}

function Set-LogText {
    param([System.Windows.Forms.RichTextBox]$Box, [string]$Text)
    if ($Box.Text -ne $Text) {
        $atEnd = $Box.SelectionStart -ge [math]::Max(0, $Box.TextLength - 2)
        $Box.Text = $Text
        if ($atEnd) {
            $Box.SelectionStart = $Box.TextLength
            $Box.ScrollToCaret()
        }
    }
}

function New-ToolbarButton {
    param(
        [string]$Text,
        [int]$Width = 105,
        [System.Drawing.Color]$BackColor = [System.Drawing.SystemColors]::Control,
        [System.Drawing.Color]$ForeColor = [System.Drawing.SystemColors]::ControlText
    )
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Width = $Width
    $button.Height = 34
    $button.Margin = New-Object System.Windows.Forms.Padding(3, 3, 3, 3)
    $button.BackColor = $BackColor
    $button.ForeColor = $ForeColor
    if ($BackColor -ne [System.Drawing.SystemColors]::Control) { $button.FlatStyle = 'Flat' }
    return $button
}

function New-LogTab {
    param([string]$Title)
    $tab = New-Object System.Windows.Forms.TabPage
    $tab.Text = $Title
    $box = New-Object System.Windows.Forms.RichTextBox
    $box.Dock = 'Fill'
    $box.ReadOnly = $true
    $box.WordWrap = $false
    $box.BackColor = [System.Drawing.Color]::FromArgb(20, 20, 20)
    $box.ForeColor = [System.Drawing.Color]::Gainsboro
    $box.Font = New-Object System.Drawing.Font('Consolas', 10)
    $tab.Controls.Add($box)
    return [pscustomobject]@{ Tab = $tab; Box = $box }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = '企业微信电脑 Agent 控制台'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(1260, 820)
$form.MinimumSize = New-Object System.Drawing.Size(960, 640)
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)

$topPanel = New-Object System.Windows.Forms.FlowLayoutPanel
$topPanel.Dock = 'Top'
$topPanel.Height = 92
$topPanel.Padding = New-Object System.Windows.Forms.Padding(10, 7, 10, 5)
$topPanel.WrapContents = $true
$topPanel.AutoScroll = $true

$startAllButton = New-ToolbarButton -Text '启动全部' -Width 105 -BackColor ([System.Drawing.Color]::FromArgb(40, 167, 69)) -ForeColor ([System.Drawing.Color]::White)
$stopAllButton = New-ToolbarButton -Text '停止全部' -Width 105
$startBridgeButton = New-ToolbarButton -Text '启动机器人' -Width 115
$stopBridgeButton = New-ToolbarButton -Text '停止机器人' -Width 115
$startAdminButton = New-ToolbarButton -Text '启动后台' -Width 105
$stopAdminButton = New-ToolbarButton -Text '停止后台' -Width 105
$openAdminButton = New-ToolbarButton -Text '打开管理后台' -Width 135 -BackColor ([System.Drawing.Color]::FromArgb(0, 120, 215)) -ForeColor ([System.Drawing.Color]::White)
$openLogsButton = New-ToolbarButton -Text '打开日志目录' -Width 125
$emergencyButton = New-ToolbarButton -Text '紧急强停' -Width 105 -BackColor ([System.Drawing.Color]::FromArgb(220, 53, 69)) -ForeColor ([System.Drawing.Color]::White)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.AutoSize = $true
$statusLabel.Margin = New-Object System.Windows.Forms.Padding(14, 9, 0, 0)
$statusLabel.Text = '正在检查 Local Supervisor…'

$topPanel.Controls.AddRange(@(
    $startAllButton,
    $stopAllButton,
    $startBridgeButton,
    $stopBridgeButton,
    $startAdminButton,
    $stopAdminButton,
    $openAdminButton,
    $openLogsButton,
    $emergencyButton,
    $statusLabel
))

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = 'Fill'
$bridgeLog = New-LogTab -Title '机器人 / Harness / Desktop 日志'
$adminLog = New-LogTab -Title '管理后台 / Supervisor 日志'
$emergencyLog = New-LogTab -Title '应急 / 旧 Java 日志（只读）'
$tabs.TabPages.AddRange(@($bridgeLog.Tab, $adminLog.Tab, $emergencyLog.Tab))
$form.Controls.Add($tabs)
$form.Controls.Add($topPanel)

function Get-ServiceUiText {
    param($ServiceStatus, [string]$FallbackName)
    if ($null -eq $ServiceStatus) { return "${FallbackName}: 未知" }
    $health = if ($null -ne $ServiceStatus.health) { [string]$ServiceStatus.health.status } else { '' }
    $pidText = if ($null -ne $ServiceStatus.pid) { " PID $($ServiceStatus.pid)" } else { '' }
    return "${FallbackName}: $($ServiceStatus.status)$pidText" + $(if (-not [string]::IsNullOrWhiteSpace($health)) { " / $health" } else { '' })
}

function Refresh-Ui {
    $status = Read-SupervisorStatus
    if ($null -eq $status) {
        $statusLabel.Text = 'Supervisor: 未运行或状态已过期'
        $statusLabel.ForeColor = [System.Drawing.Color]::DarkRed
    } else {
        $admin = $status.services.admin
        $bridge = $status.services.bridge
        $statusLabel.Text = "Supervisor: 运行中 PID $($status.supervisor.pid)    $(Get-ServiceUiText -ServiceStatus $admin -FallbackName '后台')    $(Get-ServiceUiText -ServiceStatus $bridge -FallbackName '机器人')"
        if ($bridge.status -eq 'running' -and $admin.status -eq 'running' -and $admin.health.status -in @('healthy', 'running')) {
            $statusLabel.ForeColor = [System.Drawing.Color]::DarkGreen
        } elseif ($bridge.status -eq 'running' -or $admin.status -eq 'running') {
            $statusLabel.ForeColor = [System.Drawing.Color]::DarkOrange
        } else {
            $statusLabel.ForeColor = [System.Drawing.Color]::DarkRed
        }
        if ($status.PSObject.Properties.Name -contains 'adminUiUrl' -and -not [string]::IsNullOrWhiteSpace([string]$status.adminUiUrl)) {
            $script:adminUiUrl = [string]$status.adminUiUrl
        }
    }
    Set-LogText -Box $bridgeLog.Box -Text (Read-LogTail -Paths @($bridgeErrLog, $bridgeOutLog, $desktopWorkerLog))
    Set-LogText -Box $adminLog.Box -Text (Read-LogTail -Paths @($supervisorLog, $adminErrLog, $adminOutLog))
    Set-LogText -Box $emergencyLog.Box -Text (Read-LogTail -Paths @($javaErrLog, $javaOutLog, $supervisorLog))
}

function Show-OperationError {
    param([string]$Title, [System.Management.Automation.ErrorRecord]$ErrorRecord)
    [System.Windows.Forms.MessageBox]::Show(
        $ErrorRecord.Exception.Message,
        $Title,
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
}

function Invoke-UiOperation {
    param(
        [System.Windows.Forms.Button]$Button,
        [string]$Action,
        [string]$Service,
        [string]$FailureTitle
    )
    $Button.Enabled = $false
    try {
        Invoke-SupervisorAction -Action 'start-supervisor' -DoNotAutoStart | Out-Null
        Invoke-SupervisorAction -Action $Action -Service $Service | Out-Null
    } catch {
        Show-OperationError -Title $FailureTitle -ErrorRecord $_
    } finally {
        $Button.Enabled = $true
        Refresh-Ui
    }
}

$startAllButton.Add_Click({ Invoke-UiOperation -Button $startAllButton -Action 'start' -Service 'all' -FailureTitle '启动失败' })
$stopAllButton.Add_Click({ Invoke-UiOperation -Button $stopAllButton -Action 'stop' -Service 'all' -FailureTitle '停止失败' })
$startBridgeButton.Add_Click({ Invoke-UiOperation -Button $startBridgeButton -Action 'start' -Service 'bridge' -FailureTitle '机器人启动失败' })
$stopBridgeButton.Add_Click({ Invoke-UiOperation -Button $stopBridgeButton -Action 'stop' -Service 'bridge' -FailureTitle '机器人停止失败' })
$startAdminButton.Add_Click({ Invoke-UiOperation -Button $startAdminButton -Action 'start' -Service 'admin' -FailureTitle '管理后台启动失败' })
$stopAdminButton.Add_Click({ Invoke-UiOperation -Button $stopAdminButton -Action 'stop' -Service 'admin' -FailureTitle '管理后台停止失败' })
$openAdminButton.Add_Click({
    try { Start-Process -FilePath $script:adminUiUrl }
    catch { Show-OperationError -Title '无法打开管理后台' -ErrorRecord $_ }
})
$openLogsButton.Add_Click({ Start-Process -FilePath 'explorer.exe' -ArgumentList $projectRoot })
$emergencyButton.Add_Click({
    $answer = [System.Windows.Forms.MessageBox]::Show(
        '仅会强制终止 Supervisor 已登记且身份校验通过的后台与机器人进程。是否继续？',
        '确认紧急强停',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    )
    if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
        Invoke-UiOperation -Button $emergencyButton -Action 'emergency-stop' -Service 'all' -FailureTitle '紧急强停失败'
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({ Refresh-Ui })
$timer.Start()

$form.Add_Shown({
    Refresh-Ui
    try {
        Invoke-SupervisorAction -Action 'start-supervisor' -DoNotAutoStart | Out-Null
        if (-not $NoAutoStart) {
            Invoke-SupervisorAction -Action 'start' -Service 'all' | Out-Null
        }
    } catch {
        Show-OperationError -Title '控制器启动失败' -ErrorRecord $_
    }
    Refresh-Ui
})
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })

[void]$form.ShowDialog()
