# cu_testwin.ps1 -- WinForms test window for CU regression (ASCII only)
# Controls: TextBox(Name=tbInput) + buttons 7 8 + = (btn7/btn8/btnAdd/btnEq)
#           + Label(Name=lblResult) + Button "Close"(name=btnClose)
# Result label shows the arithmetic so a test can assert real state change.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = 'cocraft CU Test Window'
$form.ClientSize = New-Object System.Drawing.Size(420, 260)
$form.StartPosition = 'CenterScreen'

$script:pending = 0
$script:current = ''

function New-Btn($text, $name, $x, $y) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Name = $name
    $b.Location = New-Object System.Drawing.Point($x, $y)
    $b.Size = New-Object System.Drawing.Size(64, 44)
    $form.Controls.Add($b)
    return $b
}

$tb = New-Object System.Windows.Forms.TextBox
$tb.Name = 'tbInput'
$tb.Location = New-Object System.Drawing.Point(20, 20)
$tb.Size = New-Object System.Drawing.Size(240, 28)
$form.Controls.Add($tb)

$lbl = New-Object System.Windows.Forms.Label
$lbl.Name = 'lblResult'
$lbl.Text = 'result: (empty)'
$lbl.AutoSize = $true
$lbl.Location = New-Object System.Drawing.Point(20, 200)
$form.Controls.Add($lbl)

$b7 = New-Btn '7' 'btn7' 20 70
$b8 = New-Btn '8' 'btn8' 100 70
$bAdd = New-Btn '+' 'btnAdd' 180 70
$bEq = New-Btn '=' 'btnEq' 260 70
$bClose = New-Btn 'Close' 'btnClose' 260 140

$b7.Add_Click({ $script:current = $script:current + '7'; $tb.Text = $script:current })
$b8.Add_Click({ $script:current = $script:current + '8'; $tb.Text = $script:current })
$bAdd.Add_Click({ $script:pending = [int]$script:current; $script:current = ''; $tb.Text = '' })
$bEq.Add_Click({
    $sum = $script:pending + [int]$script:current
    $lbl.Text = 'result: ' + $sum
})
$bClose.Add_Click({ $form.Close() })

Write-Output ('pid=' + $PID)
[void]$form.ShowDialog()
