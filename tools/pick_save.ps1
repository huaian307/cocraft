# pick_save.ps1 -- show a native Windows "save file" dialog and write the chosen path to a file.
# ASCII-only on purpose (Windows PowerShell reads BOM-less .ps1 as ANSI).
# The caller (server.py) passes -OutFile (where to write the result); an empty file means cancel.
param(
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Name = "export.txt",
    [string]$Filter = "All files (*.*)|*.*",
    [string]$Title = "Save file",
    [string]$InitialDir = ""
)

Add-Type -AssemblyName System.Windows.Forms

$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.FileName = $Name
$dialog.Filter = $Filter
$dialog.Title = $Title
$dialog.AddExtension = $true
$dialog.OverwritePrompt = $true
$dialog.CheckPathExists = $true
$dialog.ValidateNames = $true
$dialog.RestoreDirectory = $true
$ext = [System.IO.Path]::GetExtension($Name)
if ($ext) { $dialog.DefaultExt = $ext.TrimStart('.') }
if ($InitialDir -and (Test-Path -LiteralPath $InitialDir)) {
    $dialog.InitialDirectory = $InitialDir
}

# Tiny invisible topmost owner so the dialog comes to the front of the panel.
$owner = New-Object System.Windows.Forms.Form
$owner.Text = "cocraft"
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.Width = 1
$owner.Height = 1
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$owner.Opacity = 0
$owner.Show()

$result = $dialog.ShowDialog($owner)
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [System.IO.File]::WriteAllText($OutFile, $dialog.FileName)
} else {
    [System.IO.File]::WriteAllText($OutFile, "")
}

$owner.Close()
$owner.Dispose()
$dialog.Dispose()
