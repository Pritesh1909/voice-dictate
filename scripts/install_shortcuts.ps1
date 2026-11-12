# Adds a Start menu shortcut (and, with -Autostart, a Startup shortcut) for Parakeet Dictate.
# Per-user only: no admin rights needed.
param([switch]$Autostart)
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
$ico = Join-Path $env:LOCALAPPDATA 'ParakeetDictate\icon.ico'
if (-not (Test-Path $pyw)) { throw "Missing $pyw - create the venv first (see README.md)" }
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$targets = @(Join-Path $programs 'Parakeet Dictate.lnk')
if ($Autostart) { $targets += Join-Path $programs 'Startup\Parakeet Dictate.lnk' }
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '-m parakeet_dictate'
    $s.WorkingDirectory = $root
    if (Test-Path $ico) { $s.IconLocation = $ico }
    $s.Description = 'Offline dictation with Parakeet v2'
    $s.Save()
    "Created $lnk"
}
