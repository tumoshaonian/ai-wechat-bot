param(
    [Parameter(Mandatory = $true)][string]$Action,
    [Parameter(Mandatory = $true)][string]$PayloadBase64
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public sealed class NativeTopLevelWindowInfo {
    public long Handle { get; set; }
    public int ProcessId { get; set; }
    public string Title { get; set; }
    public bool Visible { get; set; }
    public int X { get; set; }
    public int Y { get; set; }
    public int Width { get; set; }
    public int Height { get; set; }
}

public static class NativeTopLevelWindowEnumerator {
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")]
    private static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint flags);
    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr hWnd, int command);

    public static NativeTopLevelWindowInfo[] Enumerate() {
        var result = new List<NativeTopLevelWindowInfo>();
        EnumWindows(delegate(IntPtr handle, IntPtr _) {
            if (!IsWindowVisible(handle)) return true;
            uint processId;
            GetWindowThreadProcessId(handle, out processId);
            if (processId == 0) return true;
            var length = Math.Max(0, GetWindowTextLength(handle));
            var title = new StringBuilder(length + 1);
            GetWindowText(handle, title, title.Capacity);
            RECT rect;
            if (!GetWindowRect(handle, out rect)) return true;
            var width = rect.Right - rect.Left;
            var height = rect.Bottom - rect.Top;
            if (width < 2 || height < 2) return true;
            result.Add(new NativeTopLevelWindowInfo {
                Handle = handle.ToInt64(),
                ProcessId = (int)processId,
                Title = title.ToString(),
                Visible = true,
                X = rect.Left,
                Y = rect.Top,
                Width = width,
                Height = height
            });
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }

    public static bool Render(long handle, IntPtr targetDc) {
        var hwnd = new IntPtr(handle);
        if (IsIconic(hwnd)) ShowWindowAsync(hwnd, 9); // SW_RESTORE
        // PW_RENDERFULLCONTENT captures Chromium even when another window
        // overlaps it.  CopyFromScreen cannot provide that guarantee.
        return PrintWindow(hwnd, targetDc, 2u);
    }
}

public static class DesktopWorkerWatchdog {
    public static void Start(int workerProcessId) {
        if (workerProcessId <= 0) return;
        var thread = new Thread(delegate() {
            try {
                var worker = Process.GetProcessById(workerProcessId);
                worker.WaitForExit();
            } catch (ArgumentException) {
                // The worker already exited before the monitor attached.
            }
            Environment.Exit(125);
        });
        thread.IsBackground = true;
        thread.Name = "DesktopWorkerWatchdog";
        thread.Start();
    }
}
'@
$script:WorkerPid = 0
$script:Stage = 'decode-request'

function Assert-WorkerAlive {
    if ($script:WorkerPid -le 0) { return }
    if ($null -eq (Get-Process -Id $script:WorkerPid -ErrorAction SilentlyContinue)) {
        throw 'desktop worker exited; action cancelled'
    }
}

function Get-Field {
    param([object]$Object, [string]$Name, [object]$Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return $Default }
    return $property.Value
}

function Contains-IgnoreCase {
    param([string]$Value, [string]$Fragment)
    if ([string]::IsNullOrWhiteSpace($Fragment)) { return $true }
    if ($null -eq $Value) { return $false }
    return $Value.IndexOf($Fragment, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Normalize-ProcessName {
    param([string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return '' }
    return [System.IO.Path]::GetFileNameWithoutExtension($Name.Trim())
}

function Get-ElementProcessName {
    param([System.Windows.Automation.AutomationElement]$Element)
    try {
        return (Get-Process -Id $Element.Current.ProcessId -ErrorAction Stop).ProcessName
    } catch {
        return ''
    }
}

function Get-Patterns {
    param([System.Windows.Automation.AutomationElement]$Element)
    $patterns = New-Object System.Collections.Generic.List[string]
    $definitions = @(
        @('Value', [System.Windows.Automation.ValuePattern]::Pattern),
        @('Invoke', [System.Windows.Automation.InvokePattern]::Pattern),
        @('Text', [System.Windows.Automation.TextPattern]::Pattern),
        @('SelectionItem', [System.Windows.Automation.SelectionItemPattern]::Pattern),
        @('ExpandCollapse', [System.Windows.Automation.ExpandCollapsePattern]::Pattern),
        @('Scroll', [System.Windows.Automation.ScrollPattern]::Pattern)
    )
    foreach ($definition in $definitions) {
        try {
            $patternObject = $null
            if ($Element.TryGetCurrentPattern($definition[1], [ref]$patternObject)) {
                $patterns.Add([string]$definition[0])
            }
        } catch {}
    }
    return @($patterns | ForEach-Object { $_ })
}

function Get-ElementValue {
    param([System.Windows.Automation.AutomationElement]$Element, [int]$Limit = 8000)
    try {
        $patternObject = $null
        if ($Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$patternObject)) {
            return [string]$patternObject.Current.Value
        }
    } catch {}
    try {
        $patternObject = $null
        if ($Element.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$patternObject)) {
            return [string]$patternObject.DocumentRange.GetText($Limit)
        }
    } catch {}
    return ''
}

function Get-ElementDescriptor {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [switch]$IncludeValue
    )
    $rectangle = $Element.Current.BoundingRectangle
    $descriptor = [ordered]@{
        process_id = $Element.Current.ProcessId
        process_name = Get-ElementProcessName -Element $Element
        native_window_handle = $Element.Current.NativeWindowHandle
        control_type = $Element.Current.ControlType.ProgrammaticName.Replace('ControlType.', '')
        name = $Element.Current.Name
        automation_id = $Element.Current.AutomationId
        class_name = $Element.Current.ClassName
        enabled = $Element.Current.IsEnabled
        offscreen = $Element.Current.IsOffscreen
        bounds = [ordered]@{
            x = [math]::Round($rectangle.X)
            y = [math]::Round($rectangle.Y)
            width = [math]::Round($rectangle.Width)
            height = [math]::Round($rectangle.Height)
        }
        patterns = @(Get-Patterns -Element $Element)
    }
    if ($IncludeValue) {
        $descriptor.value = Get-ElementValue -Element $Element
    }
    return [pscustomobject]$descriptor
}

function Get-ElementText {
    param([System.Windows.Automation.AutomationElement]$Element, [int]$Limit = 12000)
    $text = (Get-ElementValue -Element $Element -Limit $Limit).Trim()
    if (-not $text) {
        try { $text = $Element.Current.Name.Trim() } catch { $text = '' }
    }
    return $text
}

function Normalize-ComparisonText {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    return (($Text -replace '[\s\u00A0]+', '') `
        -replace '[“”]', '"' `
        -replace '[‘’]', "'")
}

function Get-MatchingNativeWindows {
    param(
        [string]$ProcessName = '',
        [string]$TitleContains = ''
    )
    $wantedProcess = Normalize-ProcessName -Name $ProcessName
    $matches = New-Object System.Collections.Generic.List[object]
    foreach ($window in [NativeTopLevelWindowEnumerator]::Enumerate()) {
        try {
            $process = Get-Process -Id $window.ProcessId -ErrorAction Stop
            $actualProcess = $process.ProcessName
            if ($wantedProcess -and -not $actualProcess.Equals($wantedProcess, [System.StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            if (-not (Contains-IgnoreCase -Value $window.Title -Fragment $TitleContains)) {
                continue
            }
            $matches.Add([pscustomobject]@{
                Native = $window
                ProcessName = $actualProcess
            })
        } catch {}
    }
    return @($matches | ForEach-Object { $_ })
}

function Get-NativeWindowDescriptor {
    param([object]$Candidate)
    $window = $Candidate.Native
    return [pscustomobject][ordered]@{
        process_id = $window.ProcessId
        process_name = $Candidate.ProcessName
        native_window_handle = $window.Handle
        control_type = 'Window'
        name = $window.Title
        automation_id = ''
        class_name = ''
        enabled = $true
        offscreen = -not $window.Visible
        bounds = [ordered]@{
            x = $window.X
            y = $window.Y
            width = $window.Width
            height = $window.Height
        }
        patterns = @()
    }
}

function Get-MatchingWindows {
    param(
        [string]$ProcessName = '',
        [string]$TitleContains = '',
        [switch]$InteractiveOnly
    )
    $matches = New-Object System.Collections.Generic.List[object]
    $nativeMatches = @(Get-MatchingNativeWindows -ProcessName $ProcessName -TitleContains $TitleContains)
    foreach ($candidate in $nativeMatches) {
        try {
            $handle = [IntPtr]([long]$candidate.Native.Handle)
            $element = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
            if ($null -eq $element) { continue }
            if ($InteractiveOnly -and ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled)) {
                continue
            }
            $matches.Add($element)
        } catch {}
    }
    return @($matches | ForEach-Object { $_ })
}

function Wait-Window {
    param(
        [string]$ProcessName,
        [string]$TitleContains,
        [double]$TimeoutSeconds = 15,
        [double]$MinimumWidth = 2,
        [double]$MinimumHeight = 2
    )
    if ([string]::IsNullOrWhiteSpace($ProcessName) -and [string]::IsNullOrWhiteSpace($TitleContains)) {
        throw 'process_name or title_contains is required to select a window'
    }
    $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        Assert-WorkerAlive
        $matches = @(Get-MatchingWindows -ProcessName $ProcessName -TitleContains $TitleContains -InteractiveOnly |
            Where-Object {
                $_.Current.BoundingRectangle.Width -ge $MinimumWidth -and
                $_.Current.BoundingRectangle.Height -ge $MinimumHeight
            })
        if ($matches.Count -gt 0) {
            # Electron applications can expose tiny helper/overlay windows under the
            # same process.  Prefer the largest visible surface so callers operate
            # on the real application window instead of an implementation detail.
            return $matches |
                Sort-Object { $_.Current.BoundingRectangle.Width * $_.Current.BoundingRectangle.Height } -Descending |
                Select-Object -First 1
        }
        Start-Sleep -Milliseconds 300
    } while ([datetime]::UtcNow -lt $deadline)
    throw "no interactive top-level window appeared for process='$ProcessName' title~'$TitleContains' within $TimeoutSeconds seconds"
}

function Matches-Control {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [object]$Selector
    )
    try {
        $controlType = [string](Get-Field -Object $Selector -Name 'control_type' -Default '')
        $nameContains = [string](Get-Field -Object $Selector -Name 'name_contains' -Default '')
        $automationId = [string](Get-Field -Object $Selector -Name 'automation_id' -Default '')
        $className = [string](Get-Field -Object $Selector -Name 'class_name' -Default '')
        $actualType = $Element.Current.ControlType.ProgrammaticName.Replace('ControlType.', '')
        if ($controlType -and -not $actualType.Equals($controlType, [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
        if (-not (Contains-IgnoreCase -Value $Element.Current.Name -Fragment $nameContains)) { return $false }
        if ($automationId -and -not $Element.Current.AutomationId.Equals($automationId, [System.StringComparison]::Ordinal)) { return $false }
        if ($className -and -not $Element.Current.ClassName.Equals($className, [System.StringComparison]::Ordinal)) { return $false }
        return $true
    } catch {
        return $false
    }
}

function Find-Control {
    param(
        [System.Windows.Automation.AutomationElement]$Window,
        [object]$Selector,
        [switch]$InteractiveOnly
    )
    $index = [int](Get-Field -Object $Selector -Name 'index' -Default 0)
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $matches = New-Object System.Collections.Generic.List[object]
    foreach ($element in $elements) {
        if (-not (Matches-Control -Element $element -Selector $Selector)) { continue }
        try {
            if ($InteractiveOnly -and ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled)) { continue }
            $matches.Add($element)
        } catch {}
    }
    if ($index -lt 0 -or $index -ge $matches.Count) {
        throw "control selector matched $($matches.Count) elements; index $index is unavailable"
    }
    return $matches[$index]
}

function Set-ControlValue {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$Value
    )
    $patternObject = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$patternObject)) {
        if ($patternObject.Current.IsReadOnly) { throw 'matched ValuePattern control is read-only' }
        $patternObject.SetValue($Value)
    } else {
        throw 'matched control does not expose a writable ValuePattern'
    }
    # Chromium contenteditable controls acknowledge SetValue before their
    # accessibility value cache catches up.  Poll the same verified control
    # briefly instead of treating the transient placeholder as a failed write.
    $deadline = [datetime]::UtcNow.AddSeconds(5)
    $actual = ''
    do {
        Start-Sleep -Milliseconds 150
        $actual = Get-ElementValue -Element $Element
        if ($actual -eq $Value) { return $actual }
    } while ([datetime]::UtcNow -lt $deadline)
    throw "value verification failed after 5 seconds; control now contains '$actual'"
}

function Invoke-Control {
    param([System.Windows.Automation.AutomationElement]$Element)
    $patternObject = $null
    if ($Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$patternObject)) {
        $patternObject.Invoke()
        return 'InvokePattern'
    }
    throw 'matched control does not expose InvokePattern'
}

function Select-DoubaoInput {
    param([System.Windows.Automation.AutomationElement]$Window)
    $inputCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Edit
    )
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $inputCondition
    )
    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($element in $elements) {
        try {
            $type = $element.Current.ControlType.ProgrammaticName.Replace('ControlType.', '')
            if ($type -ne 'Edit') { continue }
            if ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled) { continue }
            $valuePattern = $null
            if (-not $element.TryGetCurrentPattern(
                [System.Windows.Automation.ValuePattern]::Pattern,
                [ref]$valuePattern
            )) { continue }
            if ($valuePattern.Current.IsReadOnly) { continue }
            $rect = $element.Current.BoundingRectangle
            if (
                [double]::IsInfinity($rect.X) -or [double]::IsInfinity($rect.Y) -or
                $rect.Width -lt 20 -or $rect.Height -lt 15
            ) { continue }
            $score = $rect.Y + [math]::Min($rect.Width, 1000)
            if (Contains-IgnoreCase -Value $element.Current.ClassName -Fragment 'ProseMirror') { $score += 50000 }
            if (Contains-IgnoreCase -Value $element.Current.Name -Fragment '输入') { $score += 10000 }
            if (Contains-IgnoreCase -Value $element.Current.Name -Fragment '消息') { $score += 5000 }
            $candidates.Add([pscustomobject]@{ Element = $element; Score = $score })
        } catch {}
    }
    if ($candidates.Count -eq 0) {
        throw 'Doubao accessibility tree exposes no writable Edit control'
    }
    return ($candidates | Sort-Object Score -Descending | Select-Object -First 1).Element
}

function Wait-DoubaoInput {
    param(
        [System.Windows.Automation.AutomationElement]$Window,
        [double]$TimeoutSeconds
    )
    $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = 'no writable Edit control'
    do {
        Assert-WorkerAlive
        try { return Select-DoubaoInput -Window $Window } catch { $lastError = $_.Exception.Message }
        Start-Sleep -Milliseconds 300
    } while ([datetime]::UtcNow -lt $deadline)
    throw "Doubao accessibility input did not become available within $TimeoutSeconds seconds: $lastError"
}

function Resolve-DoubaoLaunchSpec {
    param([string]$LaunchPath, [string]$PreferredExecutable = '')
    if ($PreferredExecutable -and (Test-Path -LiteralPath $PreferredExecutable -PathType Leaf)) {
        return [pscustomobject]@{
            FilePath = $PreferredExecutable
            Arguments = '--force-renderer-accessibility'
            WorkingDirectory = [System.IO.Path]::GetDirectoryName($PreferredExecutable)
        }
    }
    if (-not $LaunchPath) { throw 'Doubao launcher is not configured' }
    if (-not (Test-Path -LiteralPath $LaunchPath -PathType Leaf)) {
        throw "Doubao launch path does not exist: $LaunchPath"
    }
    if ([System.IO.Path]::GetExtension($LaunchPath).Equals('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($LaunchPath)
        if (-not $shortcut.TargetPath -or -not (Test-Path -LiteralPath $shortcut.TargetPath -PathType Leaf)) {
            throw "Doubao shortcut target does not exist: $($shortcut.TargetPath)"
        }
        $arguments = @($shortcut.Arguments, '--force-renderer-accessibility') |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        return [pscustomobject]@{
            FilePath = $shortcut.TargetPath
            Arguments = ($arguments -join ' ')
            WorkingDirectory = if ($shortcut.WorkingDirectory) {
                $shortcut.WorkingDirectory
            } else {
                [System.IO.Path]::GetDirectoryName($shortcut.TargetPath)
            }
        }
    }
    return [pscustomobject]@{
        FilePath = $LaunchPath
        Arguments = '--force-renderer-accessibility'
        WorkingDirectory = [System.IO.Path]::GetDirectoryName($LaunchPath)
    }
}

function Start-DoubaoAccessible {
    param([string]$LaunchPath, [string]$PreferredExecutable = '')
    $spec = Resolve-DoubaoLaunchSpec -LaunchPath $LaunchPath -PreferredExecutable $PreferredExecutable
    $parameters = @{
        FilePath = $spec.FilePath
        ArgumentList = $spec.Arguments
    }
    if ($spec.WorkingDirectory) { $parameters.WorkingDirectory = $spec.WorkingDirectory }
    Start-Process @parameters | Out-Null
}

function Restart-DoubaoAccessible {
    param(
        [System.Windows.Automation.AutomationElement]$Window,
        [string]$LaunchPath,
        [string]$ProcessName,
        [double]$TimeoutSeconds
    )
    $processId = $Window.Current.ProcessId
    $preferredExecutable = ''
    try {
        $process = Get-Process -Id $processId -ErrorAction Stop
        $preferredExecutable = $process.Path
        if (-not $process.CloseMainWindow()) { $process.Kill() }
        if (-not $process.WaitForExit(5000)) {
            $process.Kill()
            $process.WaitForExit(5000) | Out-Null
        }
    } catch {
        throw "could not restart inaccessible Doubao process ${processId}: $($_.Exception.Message)"
    }
    Start-DoubaoAccessible -LaunchPath $LaunchPath -PreferredExecutable $preferredExecutable
    return Wait-Window `
        -ProcessName $ProcessName `
        -TitleContains '' `
        -TimeoutSeconds $TimeoutSeconds `
        -MinimumWidth 400 `
        -MinimumHeight 300
}

function Select-DoubaoSendButton {
    param([System.Windows.Automation.AutomationElement]$Window)
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button
    )
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $buttonCondition
    )
    $candidates = New-Object System.Collections.Generic.List[object]
    foreach ($element in $elements) {
        try {
            if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Button) { continue }
            if ($element.Current.IsOffscreen -or -not $element.Current.IsEnabled) { continue }
            $patterns = @(Get-Patterns -Element $element)
            if ('Invoke' -notin $patterns) { continue }
            $name = $element.Current.Name
            $automationId = $element.Current.AutomationId
            $score = 0
            if ($automationId.Equals('flow-end-msg-send', [System.StringComparison]::Ordinal)) { $score += 100000 }
            if (Contains-IgnoreCase -Value $name -Fragment '发送') { $score += 20000 }
            if (Contains-IgnoreCase -Value $name -Fragment 'send') { $score += 15000 }
            if (Contains-IgnoreCase -Value $automationId -Fragment 'send') { $score += 10000 }
            $rect = $element.Current.BoundingRectangle
            $score += $rect.Y
            $candidates.Add([pscustomobject]@{ Element = $element; Score = $score })
        } catch {}
    }
    if ($candidates.Count -eq 0) {
        throw 'Doubao window exposes no invokable Button control through UI Automation'
    }
    $selected = $candidates | Sort-Object Score -Descending | Select-Object -First 1
    if ($selected.Score -lt 10000) {
        throw 'Doubao exposes buttons, but none can be identified as the Send button'
    }
    return $selected.Element
}

function Get-TextSnapshot {
    param([System.Windows.Automation.AutomationElement]$Window)
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $items = New-Object System.Collections.Generic.List[string]
    foreach ($element in $elements) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            $type = $element.Current.ControlType.ProgrammaticName.Replace('ControlType.', '')
            if ($type -notin @('Text', 'Document', 'Edit', 'Button')) { continue }
            $text = (Get-ElementValue -Element $element).Trim()
            if (-not $text) { $text = $element.Current.Name.Trim() }
            if ($text -and $text.Length -le 12000 -and -not $items.Contains($text)) {
                $items.Add($text)
            }
        } catch {}
    }
    return @($items | ForEach-Object { $_ })
}

function Test-ElementAncestor {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$ClassContains = '',
        [string]$AutomationId = ''
    )
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $current = $Element
    for ($depth = 0; $depth -lt 12 -and $null -ne $current; $depth++) {
        try {
            if ($ClassContains -and (Contains-IgnoreCase -Value $current.Current.ClassName -Fragment $ClassContains)) {
                return $true
            }
            if ($AutomationId -and $current.Current.AutomationId.Equals($AutomationId, [System.StringComparison]::Ordinal)) {
                return $true
            }
            $current = $walker.GetParent($current)
        } catch { return $false }
    }
    return $false
}

function Get-DoubaoAnswerSnapshot {
    param(
        [System.Windows.Automation.AutomationElement]$Window,
        [string]$Question
    )
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $questionIndex = -1
    $normalizedQuestion = Normalize-ComparisonText -Text $Question
    for ($index = 0; $index -lt $elements.Count; $index++) {
        try {
            $element = $elements.Item($index)
            if ($element.Current.IsOffscreen) { continue }
            if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Text) { continue }
            if ((Normalize-ComparisonText -Text (Get-ElementText -Element $element)) -eq $normalizedQuestion) {
                $questionIndex = $index
            }
        } catch {}
    }
    $items = New-Object System.Collections.Generic.List[string]
    if ($questionIndex -ge 0) {
        for ($index = $questionIndex + 1; $index -lt $elements.Count; $index++) {
            try {
                $element = $elements.Item($index)
                if ($element.Current.IsOffscreen) { continue }
                if (
                    $element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit -and
                    (Contains-IgnoreCase -Value $element.Current.ClassName -Fragment 'ProseMirror')
                ) { break }
                if ($element.Current.ControlType -ne [System.Windows.Automation.ControlType]::Text) { continue }
                if (Test-ElementAncestor -Element $element -ClassContains 'suggest-list-item') { continue }
                if (Test-ElementAncestor -Element $element -AutomationId 'input-engine-container') { continue }
                $text = Get-ElementText -Element $element
                if (-not $text -or (Normalize-ComparisonText -Text $text) -eq $normalizedQuestion) { continue }
                if ($text -match '^(AI 生成可能有误.*|内容由AI生成|停止生成|重新生成|复制|点赞|点踩)$') { continue }
                if (-not $items.Contains($text)) { $items.Add($text) }
            } catch {}
        }
    }
    return [pscustomobject]@{
        question_found = $questionIndex -ge 0
        items = @($items | ForEach-Object { $_ })
    }
}

function Test-DoubaoGenerationActive {
    param([System.Windows.Automation.AutomationElement]$Window)
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($element in $elements) {
        try {
            if ($element.Current.IsOffscreen) { continue }
            if ($element.Current.Name -match '^(停止生成|Stop generating)$') { return $true }
        } catch {}
    }
    return $false
}

function Save-WindowScreenshot {
    param(
        [System.Windows.Automation.AutomationElement]$Window,
        [string]$Directory,
        [string]$FileName = ''
    )
    if ([string]::IsNullOrWhiteSpace($Directory)) { throw 'screenshot_directory is required' }
    [System.IO.Directory]::CreateDirectory($Directory) | Out-Null
    if ([string]::IsNullOrWhiteSpace($FileName)) {
        $FileName = 'window-' + [datetime]::Now.ToString('yyyyMMdd-HHmmss') + '.png'
    }
    $safeName = [System.IO.Path]::GetFileName($FileName)
    if (-not $safeName.EndsWith('.png', [System.StringComparison]::OrdinalIgnoreCase)) {
        $safeName += '.png'
    }
    $rect = $Window.Current.BoundingRectangle
    $width = [int][math]::Round($rect.Width)
    $height = [int][math]::Round($rect.Height)
    if ($Window.Current.IsOffscreen -or $width -lt 2 -or $height -lt 2) {
        throw 'target window is not visibly capturable'
    }
    $bitmap = New-Object System.Drawing.Bitmap($width, $height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $path = [System.IO.Path]::Combine($Directory, $safeName)
    if (Test-Path -LiteralPath $path) {
        $baseName = [System.IO.Path]::GetFileNameWithoutExtension($safeName)
        $path = [System.IO.Path]::Combine(
            $Directory,
            $baseName + '-' + [datetime]::Now.ToString('yyyyMMdd-HHmmssfff') + '.png'
        )
    }
    try {
        $handle = [long]$Window.Current.NativeWindowHandle
        if ($handle -eq 0) { throw 'target window has no native HWND for bound capture' }
        $targetDc = $graphics.GetHdc()
        try {
            if (-not [NativeTopLevelWindowEnumerator]::Render($handle, $targetDc)) {
                throw "PrintWindow failed for target HWND $handle"
            }
        } finally {
            $graphics.ReleaseHdc($targetDc)
        }
        $bitmap.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
    return [System.IO.Path]::GetFullPath($path)
}

try {
    $payloadJson = $utf8.GetString([Convert]::FromBase64String($PayloadBase64))
    $payload = if ([string]::IsNullOrWhiteSpace($payloadJson)) { [pscustomobject]@{} } else { $payloadJson | ConvertFrom-Json }
    $script:WorkerPid = [int](Get-Field -Object $payload -Name '_worker_pid' -Default 0)
    [DesktopWorkerWatchdog]::Start($script:WorkerPid)
    Assert-WorkerAlive
    $processName = [string](Get-Field -Object $payload -Name 'process_name' -Default '')
    $titleContains = [string](Get-Field -Object $payload -Name 'title_contains' -Default '')

    switch ($Action) {
        'list_windows' {
            $script:Stage = 'list-windows'
            $windows = @(Get-MatchingNativeWindows -ProcessName $processName -TitleContains $titleContains)
            $result = [ordered]@{
                ok = $true
                count = $windows.Count
                windows = @($windows | ForEach-Object { Get-NativeWindowDescriptor -Candidate $_ })
            }
        }
        'inspect_window' {
            $script:Stage = 'inspect-window'
            $window = Wait-Window -ProcessName $processName -TitleContains $titleContains -TimeoutSeconds 2
            $maxControls = [int](Get-Field -Object $payload -Name 'max_controls' -Default 120)
            $maxControls = [math]::Max(1, [math]::Min(500, $maxControls))
            $elements = $window.FindAll(
                [System.Windows.Automation.TreeScope]::Descendants,
                [System.Windows.Automation.Condition]::TrueCondition
            )
            $controls = New-Object System.Collections.Generic.List[object]
            for ($index = 0; $index -lt $elements.Count -and $controls.Count -lt $maxControls; $index++) {
                $element = $elements.Item($index)
                try {
                    $descriptor = Get-ElementDescriptor -Element $element -IncludeValue
                    if ($descriptor.name -or $descriptor.automation_id -or $descriptor.value -or $descriptor.patterns.Count -gt 0) {
                        $controls.Add($descriptor)
                    }
                } catch {}
            }
            $result = [ordered]@{
                ok = $true
                window = Get-ElementDescriptor -Element $window
                total_descendants = $elements.Count
                returned_controls = $controls.Count
                controls = @($controls | ForEach-Object { $_ })
            }
        }
        'set_value' {
            $script:Stage = 'set-value'
            $window = Wait-Window -ProcessName $processName -TitleContains $titleContains -TimeoutSeconds 2
            $control = Find-Control -Window $window -Selector $payload -InteractiveOnly
            $value = [string](Get-Field -Object $payload -Name 'value' -Default '')
            $verified = Set-ControlValue -Element $control -Value $value
            $result = [ordered]@{
                ok = $true
                action = 'set_value'
                window = Get-ElementDescriptor -Element $window
                control = Get-ElementDescriptor -Element $control -IncludeValue
                verified_value = $verified
            }
        }
        'invoke' {
            $script:Stage = 'invoke-control'
            $window = Wait-Window -ProcessName $processName -TitleContains $titleContains -TimeoutSeconds 2
            $control = Find-Control -Window $window -Selector $payload -InteractiveOnly
            $method = Invoke-Control -Element $control
            $result = [ordered]@{
                ok = $true
                action = 'invoke'
                method = $method
                window = Get-ElementDescriptor -Element $window
                control = Get-ElementDescriptor -Element $control
            }
        }
        'capture' {
            $script:Stage = 'capture-window'
            $window = Wait-Window -ProcessName $processName -TitleContains $titleContains -TimeoutSeconds 2
            $directory = [string](Get-Field -Object $payload -Name 'screenshot_directory' -Default '')
            $fileName = [string](Get-Field -Object $payload -Name 'file_name' -Default '')
            $path = Save-WindowScreenshot -Window $window -Directory $directory -FileName $fileName
            $result = [ordered]@{
                ok = $true
                window = Get-ElementDescriptor -Element $window
                screenshot_path = $path
            }
        }
        'doubao_ask' {
            $startedAt = [datetime]::UtcNow
            $question = [string](Get-Field -Object $payload -Name 'question' -Default '')
            if ([string]::IsNullOrWhiteSpace($question)) { throw 'question cannot be empty' }
            if (-not $processName) { $processName = 'Doubao' }
            $windowTimeout = [double](Get-Field -Object $payload -Name 'window_timeout_seconds' -Default 30)
            $answerTimeout = [double](Get-Field -Object $payload -Name 'answer_timeout_seconds' -Default 120)
            $stableSeconds = [double](Get-Field -Object $payload -Name 'stable_seconds' -Default 4)
            $launchPath = [string](Get-Field -Object $payload -Name 'launch_path' -Default '')
            $accessibilityRestarted = $false
            $script:Stage = 'locate-doubao-window'
            $existing = @(Get-MatchingWindows -ProcessName $processName -InteractiveOnly)
            if ($existing.Count -eq 0) {
                if (-not $launchPath) {
                    throw 'Doubao has no interactive window and DSH_DOUBAO_LAUNCH_PATH is not configured'
                }
                $script:Stage = 'launch-doubao-accessible'
                Start-DoubaoAccessible -LaunchPath $launchPath
            }
            $window = Wait-Window `
                -ProcessName $processName `
                -TitleContains '' `
                -TimeoutSeconds $windowTimeout `
                -MinimumWidth 400 `
                -MinimumHeight 300

            $script:Stage = 'locate-doubao-input'
            try {
                $input = Wait-DoubaoInput -Window $window -TimeoutSeconds ([math]::Min(3, $windowTimeout))
            } catch {
                # A normally auto-started Chromium/Electron process may keep its
                # accessibility tree disabled.  Restart only this verified
                # Doubao process with Chromium's supported accessibility flag.
                $script:Stage = 'restart-doubao-accessible'
                $window = Restart-DoubaoAccessible `
                    -Window $window `
                    -LaunchPath $launchPath `
                    -ProcessName $processName `
                    -TimeoutSeconds $windowTimeout
                $accessibilityRestarted = $true
                $script:Stage = 'wait-doubao-accessibility-tree'
                $input = Wait-DoubaoInput -Window $window -TimeoutSeconds $windowTimeout
            }

            $script:Stage = 'set-and-verify-question'
            $verifiedQuestion = Set-ControlValue -Element $input -Value $question
            $inputDescriptor = Get-ElementDescriptor -Element $input -IncludeValue
            $script:Stage = 'locate-send-control'
            $sendButton = Select-DoubaoSendButton -Window $window
            $sendDescriptor = Get-ElementDescriptor -Element $sendButton
            $script:Stage = 'invoke-send-control'
            $invokeMethod = Invoke-Control -Element $sendButton

            $script:Stage = 'verify-question-submitted'
            $submissionDeadline = [datetime]::UtcNow.AddSeconds([math]::Min(15, $answerTimeout))
            $conversation = $null
            do {
                Assert-WorkerAlive
                Start-Sleep -Milliseconds 250
                $conversation = Get-DoubaoAnswerSnapshot -Window $window -Question $question
                if ($conversation.question_found) { break }
            } while ([datetime]::UtcNow -lt $submissionDeadline)
            if ($null -eq $conversation -or -not $conversation.question_found) {
                throw 'Doubao send control was invoked, but the exact question did not appear in the conversation'
            }

            $script:Stage = 'wait-answer-stable'
            $deadline = [datetime]::UtcNow.AddSeconds($answerTimeout)
            $lastSignature = ''
            $stableSince = $null
            $answerItems = @()
            do {
                Assert-WorkerAlive
                Start-Sleep -Milliseconds 750
                $conversation = Get-DoubaoAnswerSnapshot -Window $window -Question $question
                $currentItems = @($conversation.items)
                $signature = $currentItems -join "`n---`n"
                $generationActive = Test-DoubaoGenerationActive -Window $window
                if ($currentItems.Count -gt 0 -and -not $generationActive) {
                    if ($signature -ne $lastSignature) {
                        $lastSignature = $signature
                        $stableSince = [datetime]::UtcNow
                    } elseif (
                        $null -ne $stableSince -and
                        ([datetime]::UtcNow - $stableSince).TotalSeconds -ge $stableSeconds
                    ) {
                        $answerItems = $currentItems
                        break
                    }
                } else {
                    $stableSince = $null
                }
            } while ([datetime]::UtcNow -lt $deadline)
            if ($answerItems.Count -eq 0) {
                throw "Doubao answer did not become accessible and stable within $answerTimeout seconds"
            }
            $script:Stage = 'capture-completed-doubao-window'
            $directory = [string](Get-Field -Object $payload -Name 'screenshot_directory' -Default '')
            $fileName = 'doubao-' + [datetime]::Now.ToString('yyyyMMdd-HHmmss') + '.png'
            $path = Save-WindowScreenshot -Window $window -Directory $directory -FileName $fileName
            $result = [ordered]@{
                ok = $true
                action = 'doubao_ask'
                question = $question
                verified_input = $verifiedQuestion
                submitted = $true
                submission_verified = $true
                accessibility_restarted = $accessibilityRestarted
                invoke_method = $invokeMethod
                window = Get-ElementDescriptor -Element $window
                input_control = $inputDescriptor
                send_control = $sendDescriptor
                answer_text = ($answerItems -join "`n").Trim()
                screenshot_path = $path
                elapsed_seconds = [math]::Round(([datetime]::UtcNow - $startedAt).TotalSeconds, 1)
            }
        }
        default { throw "unknown desktop action: $Action" }
    }
    $result | ConvertTo-Json -Depth 10 -Compress
} catch {
    [ordered]@{
        ok = $false
        action = $Action
        stage = $script:Stage
        error = $_.Exception.Message
        category = $_.CategoryInfo.Category.ToString()
        script_stack_trace = $_.ScriptStackTrace
    } | ConvertTo-Json -Depth 5 -Compress
}
