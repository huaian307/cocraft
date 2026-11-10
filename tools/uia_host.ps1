# uia_host.ps1 -- persistent UI Automation host (stdin/stdout JSON lines)
# NOTE: ASCII only. Must run under Windows PowerShell 5.1 (powershell.exe),
#       NOT pwsh 7 -- UIAutomationClient is .NET Framework only.
# Protocol: one JSON request per line, one JSON response per line.
#   {"id":1,"cmd":"ping"}
#   {"id":2,"cmd":"tree","hwnd":123,"depth":4,"max_nodes":300}
#   {"id":3,"cmd":"find","hwnd":123,"name":"Send","role":"Button","limit":10}
#   {"id":4,"cmd":"describe","hwnd":123}
#   {"id":5,"cmd":"click","id":"e3"}            (or find args: hwnd+name/role/automation_id)
#   {"id":6,"cmd":"setvalue","id":"e5","text":"hi"}
#   {"id":7,"cmd":"focus","id":"e5"}
#   {"id":8,"cmd":"wait","hwnd":123,"name":"Done","timeout_ms":8000}
#   {"id":9,"cmd":"shutdown"}

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
try { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$script:Elems = @{}
$script:Seq = 0
$script:Types = [System.Windows.Automation.ControlType]
$script:PatternMap = @{
    'Invoke'         = [System.Windows.Automation.AutomationElement]::IsInvokePatternAvailableProperty
    'Value'          = [System.Windows.Automation.AutomationElement]::IsValuePatternAvailableProperty
    'SelectionItem'  = [System.Windows.Automation.AutomationElement]::IsSelectionItemPatternAvailableProperty
    'ExpandCollapse' = [System.Windows.Automation.AutomationElement]::IsExpandCollapsePatternAvailableProperty
    'Toggle'         = [System.Windows.Automation.AutomationElement]::IsTogglePatternAvailableProperty
    'Text'           = [System.Windows.Automation.AutomationElement]::IsTextPatternAvailableProperty
    'Scroll'         = [System.Windows.Automation.AutomationElement]::IsScrollPatternAvailableProperty
    'RangeValue'     = [System.Windows.Automation.AutomationElement]::IsRangeValuePatternAvailableProperty
    'Window'         = [System.Windows.Automation.AutomationElement]::IsWindowPatternAvailableProperty
    'Transform'      = [System.Windows.Automation.AutomationElement]::IsTransformPatternAvailableProperty
    'Legacy'         = [System.Windows.Automation.AutomationElement]::IsLegacyIAccessiblePatternAvailableProperty
}
$script:BF = [System.Reflection.BindingFlags]'Public,Static'

# ControlType exposes its members as static properties on some builds and
# static fields on others -> index both, otherwise role lookup silently fails.
$script:RoleMap = @{}
foreach ($f in $script:Types.GetFields($script:BF)) { $script:RoleMap[$f.Name] = $f.GetValue($null) }
foreach ($p in $script:Types.GetProperties($script:BF)) { $script:RoleMap[$p.Name] = $p.GetValue($null) }

# LegacyIAccessiblePattern is missing on some .NET Framework builds; resolve it
# lazily so a missing type does not kill every click.
$script:LegacyPattern = $null
try { $script:LegacyPattern = [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern } catch { $script:LegacyPattern = $null }

function Get-ControlType($role) {
    foreach ($k in $script:RoleMap.Keys) {
        if ([string]::Equals($k, [string]$role, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $script:RoleMap[$k]
        }
    }
    throw ('unknown role: ' + $role + ' (try: Button, Edit, Pane, Text, MenuItem, ComboBox, List, ListItem, Window, Document)')
}

function Write-Resp($obj) {
    # Depth must be generous: ConvertTo-Json silently turns deeper values into
    # strings once -Depth is exceeded (that bug shows up as "str has no .get").
    $json = $obj | ConvertTo-Json -Compress -Depth 100
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

function Reg($el) {
    $script:Seq = $script:Seq + 1
    $id = 'e' + $script:Seq
    $script:Elems[$id] = $el
    return $id
}

function Get-El($id) {
    if (-not $id) { return $null }
    if ($script:Elems.ContainsKey($id)) { return $script:Elems[$id] }
    return $null
}

function Rect-Of($el) {
    try {
        $b = $el.Current.BoundingRectangle
        if ([double]::IsInfinity($b.X) -or [double]::IsInfinity($b.Y)) { return $null }
        if ([double]::IsInfinity($b.Width) -or [double]::IsInfinity($b.Height)) { return $null }
        return [ordered]@{
            x = [int]$b.X; y = [int]$b.Y;
            w = [int]$b.Width; h = [int]$b.Height
        }
    } catch { return $null }
}

function Patterns-Of($el) {
    # GetSupportedPatterns() returns everything in one call and is more reliable
    # than IsXxxPatternAvailable properties (MSAA proxy windows often report
    # False for those while Legacy is actually usable).
    try {
        $out = @()
        foreach ($p in $el.GetSupportedPatterns()) {
            $name = $p.ProgrammaticName
            if ($name -match '\.([A-Za-z]+)Pattern$') { $out += $matches[1] }
        }
        if ($out.Count -gt 0) { return $out }
    } catch { }
    $fallback = @()
    foreach ($k in $script:PatternMap.Keys) {
        try {
            if ($el.GetCurrentPropertyValue($script:PatternMap[$k])) { $fallback += $k }
        } catch { }
    }
    return $fallback
}

function Node-Of($el, $depth, $maxDepth, $maxNodes, [ref]$count) {
    $count.Value = $count.Value + 1
    $node = [ordered]@{
        id            = (Reg $el)
        name          = $el.Current.Name
        role          = ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '')
        automation_id = $el.Current.AutomationId
        class         = $el.Current.ClassName
        rect          = (Rect-Of $el)
        enabled       = [bool]$el.Current.IsEnabled
        offscreen     = [bool]$el.Current.IsOffscreen
        patterns      = @(Patterns-Of $el)
    }
    if ($depth -lt $maxDepth -and $count.Value -lt $maxNodes) {
        $kids = @()
        try {
            $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
            $c = $walker.GetFirstChild($el)
            while ($c -ne $null) {
                if ($count.Value -ge $maxNodes) { break }
                $kids += (Node-Of $c ($depth + 1) $maxDepth $maxNodes $count)
                $c = $walker.GetNextSibling($c)
            }
        } catch { }
        if ($kids.Count -gt 0) { $node['children'] = $kids }
    }
    return $node
}

function Build-Cond($req) {
    $conds = @()
    if ($req.name) {
        $conds += New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, [string]$req.name)
    }
    if ($req.automation_id) {
        $conds += New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::AutomationIdProperty, [string]$req.automation_id)
    }
    if ($req.role) {
        $ct = Get-ControlType $req.role
        $conds += New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ct)
    }
    if ($conds.Count -eq 0) { return [System.Windows.Automation.Condition]::TrueCondition }
    if ($conds.Count -eq 1) { return $conds[0] }
    # NOTE: array type MUST be Condition[] (fixed 2026-10-27).
    # AutomationCondition[] does not exist -> "type not found" for any find with 2+ conditions.
    $arr = [System.Windows.Automation.Condition[]]$conds
    return [System.Windows.Automation.AndCondition]::new($arr)
}

function Short-Node($el) {
    return [ordered]@{
        id            = (Reg $el)
        name          = $el.Current.Name
        role          = ($el.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '')
        automation_id = $el.Current.AutomationId
        class         = $el.Current.ClassName
        rect          = (Rect-Of $el)
        enabled       = [bool]$el.Current.IsEnabled
        patterns      = @(Patterns-Of $el)
    }
}

function Root-Of($hwnd) {
    if (-not $hwnd) { throw 'missing hwnd' }
    $el = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr][int]$hwnd)
    if ($el -eq $null) { throw ('window not found: hwnd=' + $hwnd) }
    return $el
}

function Find-Items($root, $req) {
    $cond = Build-Cond $req
    $limit = 10
    if ($req.limit) { $limit = [int]$req.limit }
    $like = $null
    if ($req.name_like) { $like = '*' + [string]$req.name_like + '*' }
    $clsLike = $null
    if ($req.class_like) { $clsLike = '*' + [string]$req.class_like + '*' }
    $items = @()
    $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
    $i = 0
    $scanned = 0
    foreach ($f in $found) {
        $scanned = $scanned + 1
        if ($scanned -gt 20000) { break }
        if ($i -ge $limit) { break }
        $cur = $f.Current
        if ($like -ne $null -and -not ($cur.Name -like $like)) { continue }
        if ($clsLike -ne $null -and -not ($cur.ClassName -like $clsLike)) { continue }
        $items += (Short-Node $f)
        $i = $i + 1
    }
    return $items
}

function Resolve-Target($req) {
    if ($req.element) {
        $el = Get-El ([string]$req.element)
        if ($el -eq $null) { throw ('stale element: ' + $req.element + ' (call uia_tree/find again)') }
        return $el
    }
    if ($req.hwnd) {
        $root = Root-Of $req.hwnd
        $items = @(Find-Items $root $req)
        if ($items.Count -eq 0) { throw 'no matching element' }
        $id = $items[0].id
        return (Get-El $id)
    }
    throw 'need element id or hwnd + find args'
}

function Do-Click($req) {
    $el = Resolve-Target $req
    $pat = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pat)) {
        $pat.Invoke()
        return @{ method = 'invoke' }
    }
    if ($el.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$pat)) {
        $pat.Toggle()
        return @{ method = 'toggle' }
    }
    if ($el.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pat)) {
        $pat.Select()
        return @{ method = 'select' }
    }
    if ($el.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pat)) {
        $pat.Expand()
        return @{ method = 'expand' }
    }
    # Legacy MSAA proxy (WinForms / old MFC / Delphi apps). The type itself is
    # absent on some .NET builds -> guard it and let the caller fall back to a
    # coordinate click on the element's reported rectangle.
    if ($script:LegacyPattern -ne $null) {
        try {
            if ($el.TryGetCurrentPattern($script:LegacyPattern, [ref]$pat)) {
                $pat.DoDefaultAction()
                return @{ method = 'legacy' }
            }
        } catch { }
    }
    throw 'element has no invokable pattern (fall back to coordinate click)'
}

function Do-SetValue($req) {
    $el = Resolve-Target $req
    $pat = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat)) {
        $pat.SetValue([string]$req.text)
        return @{ method = 'value' }
    }
    if ($script:LegacyPattern -ne $null) {
        try {
            if ($el.TryGetCurrentPattern($script:LegacyPattern, [ref]$pat)) {
                $pat.SetValue([string]$req.text)
                return @{ method = 'legacy' }
            }
        } catch { }
    }
    throw 'element has no ValuePattern (fall back to focus + sendkeys)'
}

function Do-Focus($req) {
    $el = Resolve-Target $req
    $el.SetFocus()
    return @{ method = 'focus' }
}

function Do-Wait($req) {
    $timeout = 8000
    if ($req.timeout_ms) { $timeout = [int]$req.timeout_ms }
    $poll = 120
    if ($req.poll_ms) { $poll = [int]$req.poll_ms }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $lastErr = ''
    while ($sw.ElapsedMilliseconds -lt $timeout) {
        try {
            $root = Root-Of $req.hwnd
            $items = @(Find-Items $root $req)
            if ($items.Count -gt 0) {
                return @{ found = $true; elapsed_ms = [int]$sw.ElapsedMilliseconds; item = $items[0] }
            }
        } catch { $lastErr = $_.Exception.Message }
        Start-Sleep -Milliseconds $poll
    }
    return @{ found = $false; elapsed_ms = [int]$sw.ElapsedMilliseconds; error = $lastErr }
}

# ---------------- main loop ----------------
while ($true) {
    $line = $null
    try { $line = [Console]::In.ReadLine() } catch { break }
    if ($line -eq $null) { break }
    if ($line.Trim() -eq '') { continue }

    $req = $null
    $id = 0
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $req = $line | ConvertFrom-Json
        # envelope id must be numeric; element id lives in its own "element"
        # field (they used to collide on "id" and killed every click)
        try { if ($req.id -ne $null) { $id = [int]$req.id } } catch { $id = 0 }
        $cmd = [string]$req.cmd

        if ($cmd -eq 'shutdown') {
            Write-Resp ([ordered]@{ id = $id; ok = $true; cmd = 'shutdown' })
            break
        }
        elseif ($cmd -eq 'ping') {
            Write-Resp ([ordered]@{ id = $id; ok = $true; pid = $PID; version = 1 })
        }
        elseif ($cmd -eq 'describe') {
            $root = Root-Of $req.hwnd
            $n = Short-Node $root
            Write-Resp ([ordered]@{ id = $id; ok = $true; window = $n; elapsed_ms = [int]$sw.ElapsedMilliseconds })
        }
        elseif ($cmd -eq 'tree') {
            $root = Root-Of $req.hwnd
            $depth = 4
            if ($req.depth) { $depth = [int]$req.depth }
            $maxNodes = 300
            if ($req.max_nodes) { $maxNodes = [int]$req.max_nodes }
            $count = 0
            $tree = Node-Of $root 0 $depth $maxNodes ([ref]$count)
            Write-Resp ([ordered]@{
                id = $id; ok = $true; root = $tree; nodes = $count;
                truncated = ($count -ge $maxNodes); elapsed_ms = [int]$sw.ElapsedMilliseconds
            })
        }
        elseif ($cmd -eq 'find') {
            $root = Root-Of $req.hwnd
            $items = @(Find-Items $root $req)
            Write-Resp ([ordered]@{ id = $id; ok = $true; items = $items; count = $items.Count; elapsed_ms = [int]$sw.ElapsedMilliseconds })
        }
        elseif ($cmd -eq 'click') {
            $r = Do-Click $req
            Write-Resp ([ordered]@{ id = $id; ok = $true; method = $r.method; elapsed_ms = [int]$sw.ElapsedMilliseconds })
        }
        elseif ($cmd -eq 'setvalue') {
            $r = Do-SetValue $req
            Write-Resp ([ordered]@{ id = $id; ok = $true; method = $r.method; chars = ([string]$req.text).Length; elapsed_ms = [int]$sw.ElapsedMilliseconds })
        }
        elseif ($cmd -eq 'focus') {
            $r = Do-Focus $req
            Write-Resp ([ordered]@{ id = $id; ok = $true; method = $r.method; elapsed_ms = [int]$sw.ElapsedMilliseconds })
        }
        elseif ($cmd -eq 'wait') {
            $r = Do-Wait $req
            $r['id'] = $id
            $r['ok'] = $true
            Write-Resp $r
        }
        else {
            Write-Resp ([ordered]@{ id = $id; ok = $false; error = ('unknown cmd: ' + $cmd) })
        }
    }
    catch {
        $msg = $_.Exception.Message
        try { Write-Resp ([ordered]@{ id = $id; ok = $false; error = $msg; elapsed_ms = [int]$sw.ElapsedMilliseconds }) } catch { }
    }
}
