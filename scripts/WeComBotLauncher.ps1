param(
    [switch]$NoAutoStart
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runtimeDir = Join-Path $projectRoot '.runtime'
$envFile = Join-Path $projectRoot '.env'
$javaJar = Join-Path $projectRoot 'target\ai-wechat-bot-0.0.1-SNAPSHOT.jar'
$bridgeExe = Join-Path $projectRoot '.venv\Scripts\wechat-aibot-bridge.exe'
$javaOutLog = Join-Path $projectRoot 'java-backend.out.log'
$javaErrLog = Join-Path $projectRoot 'java-backend.err.log'
$bridgeOutLog = Join-Path $projectRoot 'wecom-bridge.out.log'
$bridgeErrLog = Join-Path $projectRoot 'wecom-bridge.err.log'
$javaStateFile = Join-Path $runtimeDir 'java.json'
$bridgeStateFile = Join-Path $runtimeDir 'bridge.json'

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

function Get-DotEnvValue {
    param([string]$Name)
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    $match = Get-Content -LiteralPath $envFile | Where-Object {
        $_ -match ('^\s*' + [regex]::Escape($Name) + '\s*=')
    } | Select-Object -Last 1
    if ($null -eq $match) { return $null }
    $value = ($match -split '=', 2)[1].Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Save-ProcessState {
    param(
        [string]$Path,
        [System.Diagnostics.Process]$Process
    )
    @{
        pid = $Process.Id
        name = $Process.ProcessName
        startedAt = $Process.StartTime.ToUniversalTime().ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-StateProcess {
    param(
        [string]$Path,
        [string[]]$AllowedNames
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $state = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
        if ($AllowedNames -notcontains $process.ProcessName) { return $null }
        $recorded = [datetime]::Parse($state.startedAt).ToUniversalTime()
        if ([math]::Abs(($process.StartTime.ToUniversalTime() - $recorded).TotalSeconds) -gt 2) {
            return $null
        }
        return $process
    } catch {
        return $null
    }
}

function Get-JavaProcess {
    $managed = Get-StateProcess -Path $javaStateFile -AllowedNames @('java', 'javaw')
    if ($null -ne $managed) { return $managed }
    try {
        $connection = Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $connection) {
            $process = Get-Process -Id $connection.OwningProcess -ErrorAction Stop
            if ($process.ProcessName -in @('java', 'javaw')) {
                Save-ProcessState -Path $javaStateFile -Process $process
                return $process
            }
        }
    } catch {}
    return $null
}

function Get-BridgeProcess {
    $managed = Get-StateProcess -Path $bridgeStateFile -AllowedNames @('wechat-aibot-bridge', 'python', 'pythonw')
    if ($null -ne $managed) { return $managed }
    $process = Get-Process -Name 'wechat-aibot-bridge' -ErrorAction SilentlyContinue |
        Sort-Object StartTime -Descending | Select-Object -First 1
    if ($null -ne $process) {
        Save-ProcessState -Path $bridgeStateFile -Process $process
        return $process
    }
    return $null
}

function Stop-ProcessTree {
    param(
        [System.Diagnostics.Process]$RootProcess,
        [string[]]$AllowedRootNames
    )
    if ($null -eq $RootProcess -or $AllowedRootNames -notcontains $RootProcess.ProcessName) { return }
    try {
        $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        $result = Start-Process -FilePath $taskkill `
            -ArgumentList @('/PID', [string]$RootProcess.Id, '/T', '/F') `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        if ($result.ExitCode -ne 0 -and (Get-Process -Id $RootProcess.Id -ErrorAction SilentlyContinue)) {
            throw "taskkill failed with exit code $($result.ExitCode)"
        }
    } catch {
        Stop-Process -Id $RootProcess.Id -Force -ErrorAction SilentlyContinue
    }
}

function Start-JavaBackend {
    $existing = Get-JavaProcess
    if ($null -ne $existing) { return "Java already running (PID $($existing.Id))" }
    if (-not (Test-Path -LiteralPath $javaJar)) {
        throw "Runnable JAR not found: $javaJar"
    }
    $apiKey = Get-DotEnvValue -Name 'DEEPSEEK_API_KEY'
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw 'DEEPSEEK_API_KEY is missing from .env'
    }
    $previousApiKey = $env:AI_CHAT_API_KEY
    try {
        $env:AI_CHAT_API_KEY = $apiKey
        $process = Start-Process -FilePath 'java.exe' `
            -ArgumentList @('-jar', $javaJar) `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $javaOutLog `
            -RedirectStandardError $javaErrLog `
            -PassThru
    } finally {
        $env:AI_CHAT_API_KEY = $previousApiKey
    }
    Save-ProcessState -Path $javaStateFile -Process $process
    return "Java started (PID $($process.Id))"
}

function Start-PythonBridge {
    $existing = Get-BridgeProcess
    if ($null -ne $existing) { return "Bridge already running (PID $($existing.Id))" }
    if (-not (Test-Path -LiteralPath $bridgeExe)) {
        throw "Bridge executable not found: $bridgeExe"
    }
    $process = Start-Process -FilePath $bridgeExe `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $bridgeOutLog `
        -RedirectStandardError $bridgeErrLog `
        -PassThru
    Save-ProcessState -Path $bridgeStateFile -Process $process
    return "Bridge started (PID $($process.Id))"
}

function Stop-AllServices {
    $bridge = Get-BridgeProcess
    Stop-ProcessTree -RootProcess $bridge -AllowedRootNames @('wechat-aibot-bridge', 'python', 'pythonw')
    Remove-Item -LiteralPath $bridgeStateFile -Force -ErrorAction SilentlyContinue

    $java = Get-JavaProcess
    Stop-ProcessTree -RootProcess $java -AllowedRootNames @('java', 'javaw')
    Remove-Item -LiteralPath $javaStateFile -Force -ErrorAction SilentlyContinue
}

function Read-LogTail {
    param([string[]]$Paths, [int]$Tail = 250)
    $parts = @()
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) {
            $content = @(Get-Content -LiteralPath $path -Tail $Tail -ErrorAction SilentlyContinue)
            if ($content.Count -gt 0) {
                $parts += "===== $([System.IO.Path]::GetFileName($path)) ====="
                $parts += $content
            }
        }
    }
    return ($parts -join [Environment]::NewLine)
}

function Set-LogText {
    param([System.Windows.Forms.RichTextBox]$Box, [string]$Text)
    if ($Box.Text -ne $Text) {
        $Box.Text = $Text
        $Box.SelectionStart = $Box.TextLength
        $Box.ScrollToCaret()
    }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = 'WeCom Computer Agent'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(1180, 780)
$form.MinimumSize = New-Object System.Drawing.Size(900, 600)
$form.Font = New-Object System.Drawing.Font('Segoe UI', 10)

$topPanel = New-Object System.Windows.Forms.FlowLayoutPanel
$topPanel.Dock = 'Top'
$topPanel.Height = 55
$topPanel.Padding = New-Object System.Windows.Forms.Padding(10, 8, 10, 6)
$topPanel.WrapContents = $false

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = 'Start / Connect'
$startButton.Width = 145
$startButton.Height = 34
$startButton.BackColor = [System.Drawing.Color]::FromArgb(40, 167, 69)
$startButton.ForeColor = [System.Drawing.Color]::White
$startButton.FlatStyle = 'Flat'

$startLegacyJavaButton = New-Object System.Windows.Forms.Button
$startLegacyJavaButton.Text = 'Start Legacy Java'
$startLegacyJavaButton.Width = 145
$startLegacyJavaButton.Height = 34

$stopButton = New-Object System.Windows.Forms.Button
$stopButton.Text = 'STOP ALL'
$stopButton.Width = 125
$stopButton.Height = 34
$stopButton.BackColor = [System.Drawing.Color]::FromArgb(220, 53, 69)
$stopButton.ForeColor = [System.Drawing.Color]::White
$stopButton.FlatStyle = 'Flat'

$openFolderButton = New-Object System.Windows.Forms.Button
$openFolderButton.Text = 'Open Project Folder'
$openFolderButton.Width = 165
$openFolderButton.Height = 34

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.AutoSize = $true
$statusLabel.Margin = New-Object System.Windows.Forms.Padding(20, 8, 0, 0)
$statusLabel.Text = 'Checking services...'

$topPanel.Controls.AddRange(@($startButton, $startLegacyJavaButton, $stopButton, $openFolderButton, $statusLabel))

$tabs = New-Object System.Windows.Forms.TabControl
$tabs.Dock = 'Fill'

$javaTab = New-Object System.Windows.Forms.TabPage
$javaTab.Text = 'Legacy Java Log (optional)'
$javaLogBox = New-Object System.Windows.Forms.RichTextBox
$javaLogBox.Dock = 'Fill'
$javaLogBox.ReadOnly = $true
$javaLogBox.BackColor = [System.Drawing.Color]::FromArgb(20, 20, 20)
$javaLogBox.ForeColor = [System.Drawing.Color]::Gainsboro
$javaLogBox.Font = New-Object System.Drawing.Font('Consolas', 10)
$javaTab.Controls.Add($javaLogBox)

$bridgeTab = New-Object System.Windows.Forms.TabPage
$bridgeTab.Text = 'Python / WeCom / Harness Log'
$bridgeLogBox = New-Object System.Windows.Forms.RichTextBox
$bridgeLogBox.Dock = 'Fill'
$bridgeLogBox.ReadOnly = $true
$bridgeLogBox.BackColor = [System.Drawing.Color]::FromArgb(20, 20, 20)
$bridgeLogBox.ForeColor = [System.Drawing.Color]::Gainsboro
$bridgeLogBox.Font = New-Object System.Drawing.Font('Consolas', 10)
$bridgeTab.Controls.Add($bridgeLogBox)

$tabs.TabPages.AddRange(@($javaTab, $bridgeTab))
$form.Controls.Add($tabs)
$form.Controls.Add($topPanel)

function Refresh-Ui {
    $java = Get-JavaProcess
    $bridge = Get-BridgeProcess
    $javaText = if ($null -ne $java) { "Legacy Java: RUNNING (PID $($java.Id))" } else { 'Legacy Java: STOPPED (optional)' }
    $bridgeText = if ($null -ne $bridge) { "Bridge: RUNNING (PID $($bridge.Id))" } else { 'Bridge: STOPPED' }
    $statusLabel.Text = "$javaText    $bridgeText"
    $statusLabel.ForeColor = if ($null -ne $bridge) {
        [System.Drawing.Color]::DarkGreen
    } else {
        [System.Drawing.Color]::DarkRed
    }
    Set-LogText -Box $javaLogBox -Text (Read-LogTail -Paths @($javaOutLog, $javaErrLog))
    Set-LogText -Box $bridgeLogBox -Text (Read-LogTail -Paths @($bridgeErrLog, $bridgeOutLog))
}

function Start-AllFromUi {
    $startButton.Enabled = $false
    try {
        $bridgeResult = Start-PythonBridge
        $statusLabel.Text = $bridgeResult
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            'Startup failed',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } finally {
        $startButton.Enabled = $true
        Refresh-Ui
    }
}

$startButton.Add_Click({ Start-AllFromUi })
$startLegacyJavaButton.Add_Click({
    $startLegacyJavaButton.Enabled = $false
    try {
        $statusLabel.Text = Start-JavaBackend
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            $_.Exception.Message,
            'Legacy Java startup failed',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } finally {
        $startLegacyJavaButton.Enabled = $true
        Refresh-Ui
    }
})
$stopButton.Add_Click({
    $stopButton.Enabled = $false
    try {
        Stop-AllServices
    } finally {
        $stopButton.Enabled = $true
        Refresh-Ui
    }
})
$openFolderButton.Add_Click({ Start-Process -FilePath 'explorer.exe' -ArgumentList $projectRoot })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1500
$timer.Add_Tick({ Refresh-Ui })
$timer.Start()

$form.Add_Shown({
    Refresh-Ui
    if (-not $NoAutoStart) { Start-AllFromUi }
})
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })

[void]$form.ShowDialog()
